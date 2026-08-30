"""Local validation and normalization for order complaint previews."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .errors import OrderComplaintInputError

COMPLAINT_PREVIEW_TARGET = "https://pay.ldxp.cn/shopApi/Order/complaintOrder"
COMPLAINT_REASONS = frozenset({
    "不会使用",
    "无效商品",
    "涉嫌色情",
    "涉嫌赌博",
    "欺诈骗钱",
    "没人售后",
    "描述不符",
})
COMPLAINT_FIELDS = frozenset({
    "trade_no",
    "reason",
    "content",
    "contact",
    "images",
    "collect_image",
    "query_pwd",
    "email_code",
})
TRADE_NO_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,160}")
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
QUERY_PASSWORD_PATTERN = re.compile(r"[0-9]{6}")
MAX_IMAGE_URL_LENGTH = 1000
MAX_COMPLAINT_IMAGE_BYTES = 5 * 1024 * 1024
COMPLAINT_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_UPLOAD_BYTES = MAX_COMPLAINT_IMAGE_BYTES
ALLOWED_UPLOAD_MIME_TYPES = COMPLAINT_IMAGE_MIME_TYPES

# The history page renders these values directly. Keep the allowlists small
# so an upstream response cannot smuggle arbitrary fields or destinations into
# the local UI.
COMPLAINT_HISTORY_STATUS_LABELS = {
    -1: "已撤销",
    0: "待处理",
    1: "平台标记已完成",
}
COMPLAINT_MESSAGE_IDENTITY_LABELS = {
    "platform": "平台",
    "user": "商家",
    "parent": "货源商",
    "buyer": "买家",
}
MAX_HISTORY_TEXT_LENGTH = 20_000
MAX_HISTORY_REASON_LENGTH = 240
MAX_HISTORY_CONTACT_LENGTH = 254
MAX_HISTORY_IMAGES = 12
MAX_HISTORY_MESSAGES = 100
MAX_HISTORY_URL_LENGTH = 1_000
_NON_PUBLIC_HOST_SUFFIXES = (".internal", ".invalid", ".lan", ".local", ".localhost", ".test")


def _text_field(
    data: dict[str, Any],
    name: str,
    *,
    required: bool,
    max_length: int,
) -> str:
    value = data.get(name, "")
    if not isinstance(value, str):
        raise OrderComplaintInputError(f"{name} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise OrderComplaintInputError(f"{name} 不能为空")
    if len(value) > max_length:
        raise OrderComplaintInputError(f"{name} 最多允许 {max_length} 个字符")
    return value


def _image_url(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise OrderComplaintInputError(f"{field} 必须是 http(s) URL")
    value = value.strip()
    if not value or len(value) > MAX_IMAGE_URL_LENGTH or any(char.isspace() for char in value):
        raise OrderComplaintInputError(f"{field} 必须是 http(s) URL")
    try:
        parsed = urlparse(value)
        parsed.port
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except (UnicodeError, ValueError) as exc:
        raise OrderComplaintInputError(f"{field} 必须是 http(s) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or username is not None
        or password is not None
    ):
        raise OrderComplaintInputError(f"{field} 必须是 http(s) URL")
    return value


def validate_complaint_payload(
    data: Any,
) -> dict[str, Any]:
    """Return the exact payload accepted by the official complaint endpoint."""
    if not isinstance(data, dict):
        raise OrderComplaintInputError("售后申请参数必须是 JSON 对象")

    unknown_fields = sorted(str(field) for field in data if field not in COMPLAINT_FIELDS)
    if unknown_fields:
        raise OrderComplaintInputError(f"售后申请包含未知字段：{', '.join(unknown_fields)}")

    trade_no = _text_field(data, "trade_no", required=True, max_length=160)
    if not TRADE_NO_PATTERN.fullmatch(trade_no):
        raise OrderComplaintInputError("trade_no 格式无效")

    reason = _text_field(data, "reason", required=True, max_length=20)
    if reason not in COMPLAINT_REASONS:
        raise OrderComplaintInputError("reason 不是受支持的投诉类型")

    content = _text_field(data, "content", required=True, max_length=200)
    contact = _text_field(data, "contact", required=True, max_length=254)
    if not EMAIL_PATTERN.fullmatch(contact):
        raise OrderComplaintInputError("contact 必须是有效的邮箱地址")

    query_password = _text_field(data, "query_pwd", required=True, max_length=6)
    if not QUERY_PASSWORD_PATTERN.fullmatch(query_password):
        raise OrderComplaintInputError("query_pwd 必须是 6 位数字")

    email_code = _text_field(data, "email_code", required=False, max_length=32)
    if email_code:
        raise OrderComplaintInputError("当前售后流程不使用邮箱验证码")

    raw_images = data.get("images", [])
    if not isinstance(raw_images, list):
        raise OrderComplaintInputError("images 必须是 URL 数组")
    if len(raw_images) > 3:
        raise OrderComplaintInputError("images 最多允许 3 张图片")
    images = [_image_url(value, f"images[{index}]") for index, value in enumerate(raw_images)]

    collect_image = _text_field(data, "collect_image", required=False, max_length=MAX_IMAGE_URL_LENGTH)
    if collect_image:
        collect_image = _image_url(collect_image, "collect_image")

    return {
        "trade_no": trade_no,
        "reason": reason,
        "content": content,
        "contact": contact,
        "images": images,
        "collect_image": collect_image,
        "query_pwd": query_password,
        "email_code": email_code,
    }


def build_complaint_preview(data: Any) -> dict[str, Any]:
    """Validate an official complaint payload without performing any request."""
    payload = validate_complaint_payload(data)
    return {
        "submitted": False,
        "mode": "preview",
        "target": {
            "method": "POST",
            "url": COMPLAINT_PREVIEW_TARGET,
        },
        "payload": payload,
    }


def decode_complaint_upload(data: Any) -> tuple[str, str, bytes]:
    """Validate and decode a browser-selected complaint image."""
    if not isinstance(data, dict):
        raise OrderComplaintInputError("图片上传参数必须是 JSON 对象")
    # ``name``/``mime_type``/``data_base64`` are the canonical browser fields;
    # the aliases keep direct API clients interoperable without accepting URLs.
    name_key = "name" if "name" in data else "filename"
    name = _text_field(data, name_key, required=True, max_length=180)
    if any(char in name for char in "\x00\r\n/\\"):
        raise OrderComplaintInputError("图片文件名格式无效")
    mime_key = "mime_type" if "mime_type" in data else ("mime" if "mime" in data else "type")
    mime_value = data.get(mime_key, "")
    if not isinstance(mime_value, str):
        raise OrderComplaintInputError("图片格式参数无效")
    mime_type = mime_value.strip().lower()
    encoded = data.get("data_base64")
    if encoded is None:
        encoded = data.get("base64")
    if encoded is None:
        encoded = data.get("data")
    data_url = data.get("data_url")
    if encoded is None and isinstance(data_url, str):
        header, separator, encoded = data_url.partition(",")
        match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64", header.strip(), re.IGNORECASE)
        if not separator or match is None:
            raise OrderComplaintInputError("图片 data URL 格式无效")
        data_url_mime = match.group(1).lower()
        if mime_type and mime_type != data_url_mime and not (mime_type == "image/jpg" and data_url_mime == "image/jpeg"):
            raise OrderComplaintInputError("图片格式参数与 data URL 不一致")
        if not mime_type:
            mime_type = data_url_mime
    if mime_type == "image/jpg":
        mime_type = "image/jpeg"
    if mime_type not in COMPLAINT_IMAGE_MIME_TYPES:
        raise OrderComplaintInputError("图片仅支持 PNG、JPEG 或 WebP 格式")
    if not isinstance(encoded, str) or not encoded:
        raise OrderComplaintInputError("图片内容不能为空")
    if len(encoded) > ((MAX_COMPLAINT_IMAGE_BYTES + 2) // 3) * 4 + 8:
        raise OrderComplaintInputError("单张图片不能超过 5 MiB")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OrderComplaintInputError("图片内容不是有效的 Base64") from exc
    if not content or len(content) > MAX_COMPLAINT_IMAGE_BYTES:
        raise OrderComplaintInputError("单张图片不能超过 5 MiB")

    signatures = {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/webp": len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if not signatures[mime_type]:
        raise OrderComplaintInputError("图片内容与声明格式不一致")
    return name, mime_type, content


def _history_text(value: Any, limit: int) -> str:
    """Convert scalar upstream values to bounded display text."""
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _history_integer(value: Any, fallback: int = 0) -> int:
    # ``bool`` is intentionally excluded: the upstream contract uses numeric
    # status/content codes, not truthy values.
    if isinstance(value, bool) or type(value) not in (int, float, str):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _history_timestamp(value: Any) -> str:
    text = _history_text(value, 80)
    if not text:
        return ""
    try:
        stamp = int(text)
        if stamp <= 0:
            return ""
        if stamp > 10_000_000_000:
            stamp //= 1000
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return parsed.isoformat(timespec="seconds")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _history_https_url(value: Any) -> str:
    """Allow only public HTTPS URLs suitable for an image/message preview."""
    candidate = _history_text(value, MAX_HISTORY_URL_LENGTH)
    if not candidate or any(char.isspace() for char in candidate):
        return ""
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except (UnicodeError, ValueError):
        return ""
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or hostname == "localhost"
        or hostname.endswith(_NON_PUBLIC_HOST_SUFFIXES)
    ):
        return ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return ""
    if address is None and "." not in hostname:
        return ""
    return parsed.geturl()


def _history_images(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("complaint history images must be a list")
    images: list[str] = []
    seen: set[str] = set()
    for raw in value[:MAX_HISTORY_IMAGES]:
        url = _history_https_url(raw)
        if url and url not in seen:
            seen.add(url)
            images.append(url)
    return images


def _history_message(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    identity = _history_text(value.get("identity"), 24).lower()
    identity_label = COMPLAINT_MESSAGE_IDENTITY_LABELS.get(identity)
    if identity_label is None:
        return None
    content_type = _history_integer(value.get("content_type"), -1)
    if content_type == 0:
        content = _history_text(value.get("content"), MAX_HISTORY_TEXT_LENGTH)
    elif content_type == 1:
        content = _history_https_url(value.get("content"))
        if not content:
            return None
    else:
        return None
    created_at = value.get("create_time")
    if created_at is None:
        # The client contract uses ``created_at`` after its first normalization;
        # accepting it here keeps service-level validation idempotent.
        created_at = value.get("created_at")
    return {
        "identity": identity,
        "identity_label": identity_label,
        "content_type": content_type,
        "content": content,
        "created_at": _history_timestamp(created_at),
    }


def _history_data(payload: Any) -> dict[str, Any]:
    """Extract the official ``data`` object while enforcing its shape."""
    if not isinstance(payload, dict):
        raise ValueError("complaint history response must be an object")
    if "data" in payload and ("code" in payload or "msg" in payload):
        code = payload.get("code")
        try:
            valid_code = (
                type(code) in (int, float)
                and not isinstance(code, bool)
                and math.isfinite(float(code))
                and code == 1
            )
        except (OverflowError, TypeError, ValueError):
            valid_code = False
        if not valid_code:
            raise ValueError("complaint history response code is not successful")
        payload = payload.get("data")
    if not isinstance(payload, dict):
        raise ValueError("complaint history data must be an object")
    return payload


def normalize_complaint_history(
    payload: Any,
    *,
    expected_trade_no: str,
) -> dict[str, Any]:
    """Return the small, display-safe complaint history contract."""
    data = _history_data(payload)
    requested_trade_no = _history_text(expected_trade_no, 160)
    returned_trade_no = _history_text(data.get("trade_no"), 160)
    if returned_trade_no and returned_trade_no != requested_trade_no:
        raise ValueError("complaint history trade number mismatch")

    status = _history_integer(data.get("status"), -1)
    messages: list[dict[str, Any]] = []
    raw_messages = data.get("messages")
    if raw_messages is not None and not isinstance(raw_messages, list):
        raise ValueError("complaint history messages must be a list")
    for raw_message in (raw_messages or [])[:MAX_HISTORY_MESSAGES]:
        normalized = _history_message(raw_message)
        if normalized is not None:
            messages.append(normalized)

    created_at = data.get("create_time")
    if created_at is None:
        # ``OrderQueryClient`` returns the normalized contract, while injected
        # clients and fixtures may still provide the upstream field name.
        created_at = data.get("created_at")

    return {
        "status": status,
        "status_label": COMPLAINT_HISTORY_STATUS_LABELS.get(status, "未知状态"),
        "reason": _history_text(data.get("reason"), MAX_HISTORY_REASON_LENGTH),
        "content": _history_text(data.get("content"), MAX_HISTORY_TEXT_LENGTH),
        "images": _history_images(data.get("images")),
        "contact": _history_text(data.get("contact"), MAX_HISTORY_CONTACT_LENGTH),
        "created_at": _history_timestamp(created_at),
        "collect_image": _history_https_url(data.get("collect_image")),
        "messages": messages,
        "can_complaint": (
            data.get("can_complaint")
            if isinstance(data.get("can_complaint"), bool)
            else _history_integer(data.get("can_complaint"), 0) == 1
        ),
    }


# Compatibility alias matching the upstream page terminology.
normalize_complaint_info = normalize_complaint_history
