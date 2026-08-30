"""Local validation and normalization for order complaint previews."""

from __future__ import annotations

import base64
import binascii
import re
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
