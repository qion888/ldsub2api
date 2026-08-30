"""Local validation and normalization for order complaint previews."""

from __future__ import annotations

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
EMAIL_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{1,32}")
MAX_IMAGE_URL_LENGTH = 1000


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


def build_complaint_preview(data: Any) -> dict[str, Any]:
    """Validate an official complaint payload without performing any request."""
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
    if email_code and not EMAIL_CODE_PATTERN.fullmatch(email_code):
        raise OrderComplaintInputError("email_code 格式无效")

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
        "submitted": False,
        "mode": "preview",
        "target": {
            "method": "POST",
            "url": COMPLAINT_PREVIEW_TARGET,
        },
        "requirements": {
            "email_code": {
                "required_when": "order.order_complaint_email_verify == 1",
                "provided": bool(email_code),
            },
        },
        "payload": {
            "trade_no": trade_no,
            "reason": reason,
            "content": content,
            "contact": contact,
            "images": images,
            "collect_image": collect_image,
            "query_pwd": query_password,
            "email_code": email_code,
        },
    }
