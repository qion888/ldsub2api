"""Strict normalization for password-protected order details."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from monitor_core.storefront import ALLOWED_HOST

from .errors import (
    OrderQueryDetailNotFound,
    OrderQueryDetailUnavailable,
    OrderQueryPasswordInvalid,
    OrderQuerySessionExpired,
    UpstreamOrderError,
)

BASE_URL = f"https://{ALLOWED_HOST}"
MAX_TEXT_LENGTH = 20_000
MAX_LINKS = 24
MAX_LINK_LENGTH = 1_000
MAX_CARDS = 2_000
MAX_CARD_LENGTH = 16_384
MAX_CARD_BYTES = 1_500_000
MAX_STRUCTURED_DEPTH = 4
MAX_STRUCTURED_ITEMS = 100
STATUS_LABELS = {
    0: "待付款",
    1: "已付款",
    2: "已关闭",
    3: "已退款",
}
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]\r\n]{1,160})\]\(([^\s)]+)\)")
NON_PUBLIC_HOST_SUFFIXES = (".internal", ".invalid", ".lan", ".local", ".localhost", ".test")


def _safe_text(value: Any, limit: int = 240) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value or "").strip()[:limit]


def _integer(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _optional_integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _money(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return "0.00"


def _created_at(value: Any) -> str:
    text = _safe_text(value, 80)
    if not text:
        return ""
    try:
        stamp = int(text)
        if stamp < 1:
            return ""
        if stamp > 10_000_000_000:
            stamp //= 1000
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.isoformat(timespec="seconds")
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return text


def _public_hostname(value: str) -> bool:
    hostname = value.rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(NON_PUBLIC_HOST_SUFFIXES):
        return False
    try:
        return ipaddress.ip_address(hostname).is_global
    except ValueError:
        if "." not in hostname or all(char in "0123456789." for char in hostname):
            return False
        return True


def _safe_http_url(value: Any, *, same_host: bool = False) -> str:
    candidate = _safe_text(value, MAX_LINK_LENGTH)
    if not candidate:
        return ""
    parsed = urlparse(urljoin(f"{BASE_URL}/", candidate))
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not _public_hostname(parsed.hostname)
        or (same_host and parsed.hostname != ALLOWED_HOST)
    ):
        return ""
    return parsed.geturl()


def _shop_url(value: Any) -> str:
    url = _safe_http_url(value, same_host=True)
    if not url:
        return ""
    parsed = urlparse(url)
    if not re.fullmatch(r"/shop/[A-Za-z0-9_-]{3,80}/?", parsed.path):
        return ""
    return parsed._replace(query="", fragment="").geturl()


def _avatar_url(value: Any) -> str:
    url = _safe_http_url(value, same_host=True)
    if not url:
        return ""
    parsed = urlparse(url)
    path_parts = parsed.path.split("/")
    if not parsed.path.startswith("/static/") or any(part in {".", ".."} for part in path_parts):
        return ""
    return parsed._replace(query="", fragment="").geturl()


class _RichTextExtractor(HTMLParser):
    BLOCK_TAGS = {"br", "div", "li", "p", "section", "tr"}
    SKIPPED_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[dict[str, Any]] = []
        self.links_truncated = False
        self._active_links: list[int] = []
        self._skipped_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in self.SKIPPED_TAGS:
            self._skipped_depth += 1
            return
        if self._skipped_depth:
            return
        if normalized in self.BLOCK_TAGS:
            self.parts.append("\n")
        if normalized == "a" and len(self.links) < MAX_LINKS:
            href = dict(attrs).get("href") or ""
            self.links.append({"href": href, "parts": []})
            self._active_links.append(len(self.links) - 1)
        elif normalized == "a":
            self.links_truncated = True

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self.SKIPPED_TAGS:
            self._skipped_depth = max(0, self._skipped_depth - 1)
            return
        if self._skipped_depth:
            return
        if normalized == "a" and self._active_links:
            self._active_links.pop()
        if normalized in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skipped_depth:
            return
        self.parts.append(data)
        if self._active_links:
            self.links[self._active_links[-1]]["parts"].append(data)


def _rich_text(value: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, str) or not value:
        return {"text": "", "links": []}, False
    parser = _RichTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError):
        return {"text": _safe_text(value, MAX_TEXT_LENGTH), "links": []}, len(value) > MAX_TEXT_LENGTH

    parsed_text = "".join(parser.parts)
    markdown_links: list[dict[str, str]] = []
    markdown_links_truncated = False

    def replace_markdown_link(match: re.Match[str]) -> str:
        nonlocal markdown_links_truncated
        url = _safe_http_url(match.group(2))
        if url and len(markdown_links) < MAX_LINKS:
            markdown_links.append({"label": " ".join(match.group(1).split()), "url": url})
        elif url:
            markdown_links_truncated = True
        return match.group(1)

    parsed_text = MARKDOWN_LINK_PATTERN.sub(replace_markdown_link, parsed_text)
    lines = [" ".join(line.split()) for line in parsed_text.splitlines()]
    text = "\n".join(line for line in lines if line)
    truncated = (
        len(text) > MAX_TEXT_LENGTH
        or parser.links_truncated
        or markdown_links_truncated
    )
    text = text[:MAX_TEXT_LENGTH]

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parser.links:
        url = _safe_http_url(item.get("href"))
        if not url or url in seen:
            continue
        seen.add(url)
        label = " ".join("".join(item.get("parts") or []).split())[:160] or url
        links.append({"label": label, "url": url})
    for item in markdown_links:
        if len(links) >= MAX_LINKS:
            truncated = True
            break
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        links.append(item)
    return {"text": text, "links": links}, truncated


def _structured_rich_text(value: Any, *, depth: int = 0) -> tuple[dict[str, Any], bool]:
    if isinstance(value, str):
        return _rich_text(value)
    if value is None:
        return {"text": "", "links": []}, False
    if isinstance(value, (bool, int, float, Decimal)):
        return {"text": str(value), "links": []}, False
    if depth >= MAX_STRUCTURED_DEPTH:
        return {"text": "", "links": []}, True
    if not isinstance(value, (dict, list, tuple)):
        return {"text": "", "links": []}, True

    entries = list(value.items()) if isinstance(value, dict) else list(enumerate(value, 1))
    truncated = len(entries) > MAX_STRUCTURED_ITEMS
    parts: list[str] = []
    links: list[dict[str, str]] = []
    seen_links: set[str] = set()
    for key, child in entries[:MAX_STRUCTURED_ITEMS]:
        rich, child_truncated = _structured_rich_text(child, depth=depth + 1)
        truncated = truncated or child_truncated
        child_text = rich["text"]
        if child_text:
            label = _safe_text(key, 120) if isinstance(value, dict) else str(key)
            indented = child_text.replace("\n", "\n  ")
            parts.append(f"{label}: {indented}")
        for link in rich["links"]:
            if len(links) >= MAX_LINKS:
                truncated = True
                break
            if link["url"] in seen_links:
                continue
            seen_links.add(link["url"])
            links.append(link)

    text = "\n".join(parts)
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]
        truncated = True
    return {"text": text, "links": links}, truncated


def _cards(value: Any) -> tuple[list[str], bool]:
    if value is None:
        return [], False
    if not isinstance(value, list):
        raise UpstreamOrderError(
            "订单发货内容格式无效",
            code="invalid_order_detail_response",
        )
    cards: list[str] = []
    total_bytes = 0
    truncated = len(value) > MAX_CARDS
    for card in value[:MAX_CARDS]:
        if not isinstance(card, str):
            raise UpstreamOrderError(
                "订单发货内容格式无效",
                code="invalid_order_detail_response",
            )
        encoded_length = len(card.encode("utf-8"))
        if len(card) > MAX_CARD_LENGTH or total_bytes + encoded_length > MAX_CARD_BYTES:
            truncated = True
            break
        cards.append(card)
        total_bytes += encoded_length
    return cards, truncated


def _upstream_error(payload: dict[str, Any]) -> None:
    message = _safe_text(payload.get("msg") or "订单详情获取失败")
    if "安全密码错误" in message:
        raise OrderQueryPasswordInvalid()
    if "请使用联系方式重新查询" in message:
        raise OrderQuerySessionExpired()
    if "订单不存在" in message or "未找到订单" in message:
        raise OrderQueryDetailNotFound()
    raise UpstreamOrderError(
        "链动小铺未返回有效订单详情",
        code="upstream_order_detail_rejected",
    )


def normalize_order_detail(payload: Any, *, expected_trade_no: str) -> dict[str, Any]:
    """Return only fields needed by the local detail dialog."""
    if not isinstance(payload, dict):
        raise UpstreamOrderError(
            "订单详情接口响应格式无效",
            code="invalid_order_detail_response",
        )
    if payload.get("code") != 1:
        _upstream_error(payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise UpstreamOrderError(
            "订单详情接口未返回有效数据",
            code="invalid_order_detail_response",
        )

    trade_no = _safe_text(data.get("trade_no"), 160)
    if not trade_no or trade_no != expected_trade_no:
        raise UpstreamOrderError(
            "订单详情与请求的订单不匹配",
            code="invalid_order_detail_response",
        )
    status = _integer(data.get("status"), -1)
    if status != 1:
        raise OrderQueryDetailUnavailable()

    goods = data.get("goods") if isinstance(data.get("goods"), dict) else {}
    response = data.get("response") if isinstance(data.get("response"), dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    goods_type = _safe_text(goods.get("goods_type"), 30).lower()
    instructions, _ = _rich_text(
        (goods.get("extend") or {}).get("instructions")
        if isinstance(goods.get("extend"), dict)
        else ""
    )
    delivery_content, content_truncated = _structured_rich_text(response.get("api_data"))
    cards, cards_truncated = _cards(response.get("cards"))

    return {
        "trade_no": trade_no,
        "goods_name": _safe_text(data.get("goods_name"), 240),
        "goods_key": _safe_text(goods.get("goods_key"), 80),
        "goods_type": goods_type,
        "status": status,
        "status_label": STATUS_LABELS.get(status, "未知状态"),
        "quantity": max(0, _integer(data.get("quantity"))),
        "sendout": max(0, _integer(data.get("sendout"))),
        "total_amount": _money(data.get("total_amount")),
        "created_at": _created_at(data.get("create_time")),
        "success_at": _created_at(data.get("success_time")),
        "coupon_used": _integer(data.get("use_coupon")) == 1,
        "coupon_price": _money(data.get("coupon_price")),
        "can_complaint": _integer(data.get("can_complaint")) == 1,
        "need_query_password": _integer(data.get("need_query_password")) == 1,
        "contact": _safe_text(data.get("contact"), 240),
        "seller": {
            "nickname": _safe_text(user.get("nickname"), 160),
            "avatar": _avatar_url(user.get("avatar")),
            "shop_url": _shop_url(user.get("link")),
            "contact_qq": _safe_text(user.get("contact_qq"), 160),
            "contact_mobile": _safe_text(user.get("contact_mobile"), 160),
            "contact_wechat": _safe_text(user.get("contact_wechat"), 500),
        },
        "instructions": instructions,
        "delivery": {
            "kind": goods_type,
            "cards": cards,
            "api_status": _optional_integer(response.get("api_status")),
            "message": _safe_text(response.get("api_msg"), 2_000),
            "content": delivery_content["text"],
            "links": delivery_content["links"],
            "truncated": cards_truncated or content_truncated,
        },
    }
