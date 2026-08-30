from __future__ import annotations

import html
import base64
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from monitor_settings import (
    DEFAULT_INTERVAL,
    DEFAULT_SHOP_INTERVAL,
    MAX_INTERVAL,
    MIN_INTERVAL,
    MonitorNotFound,
    normalize_interval,
    sync_shop_product_intervals,
    update_shop_monitoring,
    update_watch_monitoring,
)
from monitor_core import database as monitor_database
from monitor_core import history as monitor_history
from monitor_core import settings as monitor_setting_store
from monitor_core.browser_verification import BrowserVerificationManager as CoreBrowserVerificationManager
from monitor_core.workers import MonitorWorker as CoreMonitorWorker
from sub2api import automation as sub2api_automation
from sub2api import client as sub2api_client
from sub2api import payloads as sub2api_payloads
from sub2api import reclaim as sub2api_reclaim
from sub2api import routes as sub2api_routes
from sub2api import settings as sub2api_config
from sub2api import worker as sub2api_worker
from sub2api.constants import CODEX_FINGERPRINT_MODES, DEFAULT_AUTOMATION, DEFAULT_URL

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


HOST = os.environ.get("LDXP_HOST", "127.0.0.1")
PORT = int(os.environ.get("LDXP_PORT", "8000"))
FRONTEND_URL = os.environ.get("LDXP_FRONTEND_URL", "http://127.0.0.1:5173/")
DB_PATH = Path(os.environ.get("LDXP_DB_PATH", str(Path(__file__).with_name("monitor.db"))))
ALLOWED_HOST = "pay.ldxp.cn"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = "LDXP-Local-Monitor/2.0"
VISITOR_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{6,80}")
WAF_MARKERS = (b"aliyunCaptcha", b"aliyunCaptcha-sliding-slider", b"waf_nc", b"u_atoken")
UNLISTED_ERROR_MARKERS = ("商品未上架", "商品不存在", "已下架", "已不在店铺列表")
DEFAULT_REDEEM_URL = "https://30d.team"
DEFAULT_SUB2API_URL = DEFAULT_URL
DEFAULT_SUB2API_AUTOMATION = DEFAULT_AUTOMATION
MAX_EXTERNAL_JSON_BYTES = 8 * 1024 * 1024
MAX_REQUEST_JSON_BYTES = 32 * 1024 * 1024


class WafChallengeRequired(RuntimeError):
    pass


class PreorderConflict(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database() -> sqlite3.Connection:
    return monitor_database.create_database(DB_PATH)


def init_database() -> None:
    monitor_database.initialize_database(
        database,
        now=utc_now,
        default_interval=DEFAULT_INTERVAL,
        default_redeem_url=DEFAULT_REDEEM_URL,
        default_sub2api_url=DEFAULT_SUB2API_URL,
        default_automation=DEFAULT_SUB2API_AUTOMATION,
    )


class DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: Any) -> str:
    parser = DescriptionParser()
    parser.feed(html.unescape(str(value or "")))
    return " ".join(" ".join(parser.parts).split())


def parse_item_url(value: str) -> tuple[str, str]:
    parsed = urlparse((value or "").strip())
    match = re.fullmatch(r"/item/([A-Za-z0-9_-]{3,80})/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST or not match:
        raise ValueError("仅支持 https://pay.ldxp.cn/item/商品编号 格式的商品链接")
    goods_key = match.group(1)
    return goods_key, f"https://{ALLOWED_HOST}/item/{goods_key}"


def parse_shop_url(value: str) -> tuple[str, str]:
    parsed = urlparse((value or "").strip())
    match = re.fullmatch(r"/shop/([A-Za-z0-9_-]{3,80})/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST or not match:
        raise ValueError("仅支持 https://pay.ldxp.cn/shop/店铺Token 格式的店铺链接")
    token = match.group(1)
    return token, f"https://{ALLOWED_HOST}/shop/{token}"


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


def _stock_value(item: dict[str, Any]) -> Any:
    keys = (
        "stock",
        "inventory",
        "inventory_count",
        "stock_count",
        "card_count",
        "cards_count",
        "surplus",
        "surplus_count",
        "quantity",
        "remain",
        "remaining",
    )
    containers = [item]
    for key in ("extend", "inventory_info", "stock_info"):
        value = item.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        value = _first_value(container, keys)
        if value not in (None, ""):
            return value
    return None


def _money(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value))
        return f"{number:.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def _compact_number(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value))
        return format(number.normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def _upstream_enabled(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def commerce_tags_from_goods(item: dict[str, Any]) -> list[dict[str, str]]:
    extend = item.get("extend") if isinstance(item.get("extend"), dict) else {}
    tags: list[dict[str, str]] = []

    multiple = item.get("multipleoffers") if isinstance(item.get("multipleoffers"), dict) else {}
    multiple_rules = multiple.get("rules") if isinstance(multiple.get("rules"), list) else []
    if _upstream_enabled(multiple.get("available")) and multiple_rules:
        details = []
        discount_type = str(multiple.get("discount_type") or "")
        for rule in multiple_rules:
            if not isinstance(rule, dict):
                continue
            condition = _compact_number(rule.get("condition"))
            value = _compact_number(rule.get("value"))
            if not condition or not value:
                continue
            if discount_type == "1":
                details.append(f"购{condition}件享{value}折")
            elif discount_type == "2":
                details.append(f"购{condition}件减{value}元")
            else:
                details.append(f"购{condition}件优惠{value}")
        tags.append({
            "key": "multiple_offers",
            "label": "多件折扣",
            "tone": "promotion",
            "detail": "；".join(details) or "购买多件可享优惠",
        })

    discount = item.get("discount") if isinstance(item.get("discount"), dict) else {}
    if _upstream_enabled(discount.get("available")):
        rebate = _compact_number(discount.get("rebate"))
        tags.append({
            "key": "discount",
            "label": "折扣优惠",
            "tone": "promotion",
            "detail": f"当前享{rebate}折" if rebate else "当前商品参与折扣",
        })

    fullgift = item.get("fullgift") if isinstance(item.get("fullgift"), dict) else {}
    gift_rules = fullgift.get("rules") if isinstance(fullgift.get("rules"), list) else []
    if _upstream_enabled(fullgift.get("available")):
        details = []
        for rule in gift_rules:
            if not isinstance(rule, dict):
                continue
            condition = _compact_number(rule.get("condition"))
            value = _compact_number(rule.get("value"))
            if condition and value:
                details.append(f"购{condition}件赠{value}件")
        tags.append({
            "key": "full_gift",
            "label": "满件赠送",
            "tone": "promotion",
            "detail": "；".join(details) or "达到门槛可获赠品",
        })

    if str(item.get("goods_type") or "").strip().lower() == "card":
        tags.append({
            "key": "delivery",
            "label": "自动发货",
            "tone": "success",
            "detail": "卡密商品付款后由平台自动交付",
        })

    limit_value = _first_value(extend, ("limit_count", "limit"))
    try:
        minimum = max(1, int(limit_value or 1))
    except (TypeError, ValueError):
        minimum = 1
    tags.append({
        "key": "minimum",
        "label": f"{minimum}件起购",
        "tone": "info",
        "detail": f"单次购买数量不得少于{minimum}件",
    })

    if _upstream_enabled(item.get("coupon_status")):
        tags.append({
            "key": "coupon",
            "label": "支持优惠券",
            "tone": "offer",
            "detail": "结算时可输入有效优惠券",
        })

    if _upstream_enabled(extend.get("query_password_status")):
        tags.append({
            "key": "query_password",
            "label": "密码保护",
            "tone": "secure",
            "detail": "查询订单或领取卡密时需要安全密码",
        })
    return tags


def is_unlisted_error(value: Any) -> bool:
    message = str(value or "")
    return any(marker in message for marker in UNLISTED_ERROR_MARKERS)


def normalize_goods_payload(payload: dict[str, Any], goods_key: str) -> dict[str, Any]:
    if payload.get("code") != 1 or not isinstance(payload.get("data"), dict):
        message = str(payload.get("msg") or "商品接口未返回有效数据")
        raise RuntimeError(message[:160])

    item = payload["data"]
    extend = item.get("extend") if isinstance(item.get("extend"), dict) else {}
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    seller = item.get("user") if isinstance(item.get("user"), dict) else {}
    limit_count = extend.get("limit_count")
    sale_status = "on_sale" if item.get("status") == 1 else "off_sale"
    stock_value = _stock_value(item) if sale_status == "on_sale" else None
    specs = {
        "商品编号": goods_key,
        "商品类型": item.get("goods_type") or "未知",
        "商品分类": category.get("name") or "未分类",
        "最低起购": limit_count if limit_count not in (None, "", 0) else 1,
        "联系方式": item.get("contact_format") or "任意",
        "查询密码": "需要" if _upstream_enabled(extend.get("query_password_status")) else "不需要",
        "店铺": seller.get("nickname") or "链动小铺",
    }
    return {
        "goods_key": goods_key,
        "title": str(item.get("name") or f"链动小铺商品 {goods_key}"),
        "price": _money(item.get("real_price") if item.get("real_price") not in (None, "") else item.get("price")),
        "market_price": _money(item.get("market_price")),
        "stock": stock_value,
        "stock_label": (
            str(stock_value) if stock_value is not None else "接口未公开数量"
        ) if sale_status == "on_sale" else "未上架",
        "sale_status": sale_status,
        "description": plain_text(item.get("description")),
        "image": str(item.get("image") or ""),
        "specs": specs,
        "limit_count": int(limit_count) if str(limit_count or "").isdigit() else None,
        "contact_format": str(item.get("contact_format") or "any"),
        "query_password_required": _upstream_enabled(extend.get("query_password_status")),
        "commerce_tags": commerce_tags_from_goods(item),
        "source_url": str(item.get("link") or f"https://{ALLOWED_HOST}/item/{goods_key}"),
        "raw_data": item,
    }


def _post_shop_api(
    endpoint: str,
    payload: dict[str, Any],
    referer: str,
    *,
    visitor_id: str | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": f"https://{ALLOWED_HOST}",
        "Referer": referer,
    }
    if visitor_id:
        headers["Visitorid"] = visitor_id
    request = Request(
        f"https://{ALLOWED_HOST}{endpoint}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("接口响应过大")
    except HTTPError as exc:
        raise RuntimeError(f"链动小铺接口返回 HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("无法连接链动小铺接口") from exc
    if "text/html" in content_type.lower() or any(marker in raw for marker in WAF_MARKERS):
        if any(marker in raw for marker in WAF_MARKERS):
            raise WafChallengeRequired("链动小铺触发阿里云 WAF 滑块验证，请使用浏览器验证后同步")
        raise RuntimeError("链动小铺接口返回了 HTML 页面，不是商品 JSON")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("链动小铺接口返回了无法解析的数据") from exc
    if not isinstance(result, dict):
        raise RuntimeError("链动小铺接口响应格式无效")
    return result


def _random_visitor_id() -> str:
    """Match the storefront's short localStorage visitor id shape."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = uuid.uuid4().int
    chars: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        chars.append(alphabet[remainder])
    return "".join(reversed(chars))[-16:] or "visitor"


def fetch_buyer_juuid(shop_token: str) -> dict[str, Any]:
    """Resolve the juuid posted by buyerBlackIframe for a shop token."""
    token = str(shop_token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", token):
        raise ValueError("店铺 Token 格式无效")
    referer = f"https://{ALLOWED_HOST}/shop/{token}"
    script_request = Request(
        f"https://{ALLOWED_HOST}/shopApi/Shop/buyerBlackJs?token={token}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/javascript, */*;q=0.8",
            "Referer": referer,
        },
        method="GET",
    )
    try:
        with urlopen(script_request, timeout=15) as response:
            script = response.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", "replace")
    except (HTTPError, URLError, UnicodeError) as exc:
        raise RuntimeError("无法获取店铺支付身份脚本") from exc
    iframe_match = re.search(r"iframe\.src\s*=\s*['\"]([^'\"]+)['\"]", script)
    iframe_url = iframe_match.group(1) if iframe_match else f"https://{ALLOWED_HOST}/shopApi/common/buyerBlackIframe"
    iframe_request = Request(
        iframe_url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html, */*;q=0.8", "Referer": referer},
        method="GET",
    )
    try:
        with urlopen(iframe_request, timeout=15) as response:
            iframe = response.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", "replace")
    except (HTTPError, URLError, UnicodeError) as exc:
        raise RuntimeError("无法获取支付身份 iframe") from exc
    juuid_match = re.search(r"(?:const\s+juuid|juuid)\s*=\s*['\"]([A-Za-z0-9_-]{1,32})['\"]", iframe)
    if not juuid_match:
        raise RuntimeError("支付身份脚本未返回有效 juuid")
    return {
        "juuid": juuid_match.group(1),
        "source": "buyerBlackJs -> buyerBlackIframe -> postMessage(CloudBuyerBlack)",
        "iframe_url": iframe_url,
    }


def fetch_payment_channels(shop_token: str) -> list[dict[str, Any]]:
    token = str(shop_token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", token):
        raise ValueError("店铺 Token 格式无效")
    result = _post_shop_api(
        "/shopApi/Shop/getUserChannel",
        {"token": token},
        f"https://{ALLOWED_HOST}/shop/{token}",
        visitor_id=_random_visitor_id(),
    )
    if result.get("code") != 1 or not isinstance(result.get("data"), list):
        raise RuntimeError(str(result.get("msg") or "无法获取可用支付渠道")[:200])
    channels = []
    for channel in result["data"]:
        if not isinstance(channel, dict) or channel.get("status") not in (None, 1, True, "1"):
            continue
        try:
            channel_id = int(channel.get("id"))
        except (TypeError, ValueError):
            continue
        if channel_id < 1:
            continue
        channels.append(
            {
                "id": channel_id,
                "name": str(channel.get("show_name") or channel.get("name") or channel.get("code") or channel_id),
                "code": str(channel.get("code") or ""),
            }
        )
    return channels


def create_official_payment_order(
    *,
    goods_key: str,
    quantity: int,
    coupon_code: str,
    channel_id: int,
    contact: str,
    query_password: str,
    select_cards_ids: list[Any] | None,
    juuid: str,
    referer: str,
    visitor_id: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", goods_key):
        raise ValueError("商品编号格式无效")
    if quantity < 1 or quantity > 99:
        raise ValueError("购买数量应为 1 到 99")
    if channel_id < 1 or channel_id > 99:
        raise ValueError("支付渠道编号无效")
    contact = str(contact or "").strip()
    if not contact:
        raise ValueError("请填写联系方式")
    query_password = str(query_password or "")
    juuid = str(juuid or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", juuid):
        raise ValueError("juuid 格式无效，请重新获取")
    if not re.fullmatch(rf"https://{re.escape(ALLOWED_HOST)}/item/[A-Za-z0-9_-]{{3,80}}", referer):
        referer = f"https://{ALLOWED_HOST}/item/{goods_key}"
    visitor_id = str(visitor_id or "").strip()
    if not VISITOR_ID_PATTERN.fullmatch(visitor_id):
        visitor_id = _random_visitor_id()
    payload = {
        "goods_key": goods_key,
        "quantity": quantity,
        "coupon_code": str(coupon_code or ""),
        "channel_id": channel_id,
        "contact": contact,
        "query_password": query_password,
        "select_cards_ids": select_cards_ids if isinstance(select_cards_ids, list) else [],
        "extend": {"juuid": juuid},
    }
    result = _post_shop_api(
        "/shopApi/Pay/order",
        payload,
        referer,
        visitor_id=visitor_id,
    )
    if result.get("code") != 1 or not isinstance(result.get("data"), dict):
        raise RuntimeError(str(result.get("msg") or "官方支付接口返回失败")[:200])
    data = result["data"]
    trade_no = str(data.get("trade_no") or "").strip()
    payurl = str(data.get("payurl") or data.get("pay_url") or "").strip()
    if not trade_no or not payurl or not payurl.startswith(f"https://{ALLOWED_HOST}/"):
        raise RuntimeError("官方支付接口未返回有效支付链接")
    return {
        "mode": "official",
        "status": "created",
        "trade_no": trade_no,
        "amount": _money(data.get("total_amount")),
        "channel_id": channel_id,
        "channel": "alipay" if channel_id == 1 else "wechat",
        "payment_url": payurl,
        "juuid": juuid,
        "notice": "官方订单已创建。请在弹出的支付页面核对金额并完成支付。",
    }


def fetch_goods(url: str) -> dict[str, Any]:
    goods_key, canonical_url = parse_item_url(url)
    payload = _post_shop_api(
        "/shopApi/Shop/goodsInfo",
        {"goods_key": goods_key, "trade_no": ""},
        canonical_url,
    )
    return normalize_goods_payload(payload, goods_key)


def _goods_list_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if payload.get("code") != 1:
        raise RuntimeError(str(payload.get("msg") or "店铺商品接口返回失败")[:160])
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)], {}
    if not isinstance(data, dict):
        raise RuntimeError("店铺商品接口未返回列表数据")
    for key in ("list", "items", "rows", "records", "data", "goods"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)], data
    return [], data


def normalize_goods_list_item(item: dict[str, Any], shop_token: str) -> dict[str, Any]:
    goods_key = str(
        _first_value(item, ("goods_key", "key", "goodsKey", "item_key")) or ""
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", goods_key):
        link = str(item.get("link") or item.get("url") or "")
        try:
            goods_key, _ = parse_item_url(link)
        except ValueError as exc:
            raise ValueError("店铺列表商品缺少有效 goods_key") from exc
    extend = item.get("extend") if isinstance(item.get("extend"), dict) else {}
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    seller = item.get("user") if isinstance(item.get("user"), dict) else {}
    limit_count = _first_value(extend, ("limit_count", "limit"))
    sales = _first_value(item, ("sales", "sales_count", "sold", "sale_count"))
    status_value = _first_value(item, ("status", "goods_status", "is_sale"))
    sale_status = "on_sale" if status_value is None or str(status_value).lower() in {"1", "true", "on_sale"} else "off_sale"
    stock_value = _stock_value(item) if sale_status == "on_sale" else None
    specs = {
        "商品编号": goods_key,
        "商品类型": item.get("goods_type") or "未知",
        "商品分类": category.get("name") or item.get("category_name") or "未分类",
        "最低起购": limit_count if limit_count not in (None, "", 0) else 1,
        "查询密码": "需要" if _upstream_enabled(extend.get("query_password_status")) else "未知",
        "店铺": seller.get("nickname") or shop_token,
        "店铺Token": shop_token,
    }
    if sales not in (None, ""):
        specs["累计销量"] = sales
    return {
        "goods_key": goods_key,
        "title": str(item.get("name") or item.get("title") or f"链动小铺商品 {goods_key}"),
        "price": _money(item.get("real_price") if item.get("real_price") not in (None, "") else item.get("price")),
        "market_price": _money(item.get("market_price")),
        "stock": stock_value,
        "stock_label": (
            str(stock_value) if stock_value not in (None, "") else "接口未公开数量"
        ) if sale_status == "on_sale" else "未上架",
        "sale_status": sale_status,
        "description": plain_text(item.get("description")),
        "image": str(item.get("image") or item.get("cover") or ""),
        "specs": specs,
        "limit_count": int(limit_count) if str(limit_count or "").isdigit() else None,
        "contact_format": str(item.get("contact_format") or "any"),
        "query_password_required": _upstream_enabled(extend.get("query_password_status")),
        "commerce_tags": commerce_tags_from_goods(item),
        "source_url": f"https://{ALLOWED_HOST}/item/{goods_key}",
        "raw_data": item,
    }


def fetch_shop_catalog(
    shop_url: str,
    *,
    keywords: str = "",
    category_id: int | None = None,
    goods_type: str = "card",
    page_size: int = 50,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    token, canonical_url = parse_shop_url(shop_url)
    products: dict[str, dict[str, Any]] = {}
    for current in range(1, max_pages + 1):
        request_data = {
            "token": token,
            "keywords": keywords,
            "category_id": category_id or "",
            "goods_type": goods_type,
            "current": current,
            "pageSize": page_size,
        }
        payload = _post_shop_api("/shopApi/Shop/goodsList", request_data, canonical_url)
        rows, pagination = _goods_list_rows(payload)
        for row in rows:
            product = normalize_goods_list_item(row, token)
            products[product["goods_key"]] = product

        total_value = _first_value(pagination, ("total", "count", "total_count"))
        last_page = _first_value(pagination, ("last_page", "lastPage", "pages", "page_count"))
        try:
            total = int(total_value) if total_value not in (None, "") else None
        except (TypeError, ValueError):
            total = None
        try:
            page_count = int(last_page) if last_page not in (None, "") else None
        except (TypeError, ValueError):
            page_count = None

        if not rows:
            break
        if total is not None and current * page_size >= total:
            break
        if page_count is not None and current >= page_count:
            break
        if len(rows) < page_size:
            break
    return list(products.values())


def _json_value(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def serialize_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["specs"] = _json_value(result.get("specs"), {})
    result["raw_data"] = _json_value(result.get("raw_data"), {})
    raw_stock = result.get("stock")
    result["stock"] = int(raw_stock) if str(raw_stock or "").isdigit() else None
    result["stock_label"] = str(raw_stock) if raw_stock not in (None, "") else "接口未公开数量"
    if result.get("sale_status") == "off_sale":
        result["stock"] = None
        result["stock_label"] = "未上架"
    result["query_password_required"] = result["specs"].get("查询密码") == "需要"
    legacy_limit = result["specs"].pop("单次限购", None)
    if "最低起购" not in result["specs"] and legacy_limit not in (None, ""):
        result["specs"]["最低起购"] = legacy_limit
    limit_value = result["specs"].get("最低起购")
    result["limit_count"] = int(limit_value) if str(limit_value).isdigit() else None
    commerce_source = dict(result["raw_data"])
    commerce_extend = commerce_source.get("extend") if isinstance(commerce_source.get("extend"), dict) else {}
    commerce_extend = dict(commerce_extend)
    if result["limit_count"] is not None:
        commerce_extend.setdefault("limit_count", result["limit_count"])
    if result["query_password_required"]:
        commerce_extend.setdefault("query_password_status", 1)
    commerce_source["extend"] = commerce_extend
    result["commerce_tags"] = commerce_tags_from_goods(commerce_source)
    return result


def effective_watch_product(
    connection: sqlite3.Connection,
    watch_id: int,
    latest: sqlite3.Row | None = None,
    attempt: sqlite3.Row | None = None,
) -> dict[str, Any] | None:
    if latest is None:
        latest = connection.execute(
            "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
            (watch_id,),
        ).fetchone()
    product = serialize_snapshot(latest)
    if product is None:
        return None
    if attempt is None:
        attempt = connection.execute(
            "SELECT fetched_at, status, error FROM snapshots WHERE watch_id = ? ORDER BY id DESC LIMIT 1",
            (watch_id,),
        ).fetchone()
    shop_state = connection.execute(
        """
        SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN listed = 1 THEN 1 ELSE 0 END), 0) AS listed
        FROM shop_products WHERE watch_id = ?
        """,
        (watch_id,),
    ).fetchone()
    removed_from_catalog = bool(shop_state and shop_state["total"] and not shop_state["listed"])
    explicit_unlisted = bool(
        attempt and attempt["status"] == "error" and is_unlisted_error(attempt["error"])
    )
    if product.get("sale_status") != "on_sale" or explicit_unlisted or removed_from_catalog:
        product["sale_status"] = "off_sale"
        product["stock"] = None
        product["stock_label"] = "未上架"
    return product


def record_fetch(watch_id: int) -> dict[str, Any]:
    stamp = utc_now()
    with database() as connection:
        watch = connection.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
    if watch is None:
        raise KeyError("监控商品不存在")
    try:
        data = fetch_goods(watch["url"])
        with database() as connection:
            previous = connection.execute(
                "SELECT price FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                (watch_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO snapshots(
                    watch_id, title, price, market_price, stock, description, specs,
                    image, sale_status, goods_key, raw_data, fetched_at, status, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', NULL)
                """,
                (
                    watch_id,
                    data["title"],
                    data["price"],
                    data["market_price"],
                    None if data["stock"] is None else str(data["stock"]),
                    data["description"],
                    json.dumps(data["specs"], ensure_ascii=False),
                    data["image"],
                    data["sale_status"],
                    data["goods_key"],
                    json.dumps(data["raw_data"], ensure_ascii=False),
                    stamp,
                ),
            )
            connection.execute("UPDATE watches SET last_run = ? WHERE id = ?", (stamp, watch_id))
        old_price = previous["price"] if previous else None
        data["previous_price"] = old_price
        data["price_changed"] = old_price not in (None, "") and old_price != data["price"]
        data.update({"fetched_at": stamp, "status": "success"})
        data.pop("raw_data", None)
        return data
    except Exception as exc:
        error = str(exc)[:240] or "抓取失败"
        with database() as connection:
            connection.execute(
                "INSERT INTO snapshots(watch_id, fetched_at, status, error) VALUES(?, ?, 'error', ?)",
                (watch_id, stamp, error),
            )
            connection.execute("UPDATE watches SET last_run = ? WHERE id = ?", (stamp, watch_id))
        raise RuntimeError(error) from exc


def record_shop_fetch(shop_id: int, products_override: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    stamp = utc_now()
    with database() as connection:
        shop = connection.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
    if shop is None:
        raise KeyError("监控店铺不存在")
    try:
        products = products_override
        if products is None:
            products = fetch_shop_catalog(
                shop["url"],
                keywords=shop["keywords"] or "",
                category_id=shop["category_id"],
                goods_type=shop["goods_type"] or "card",
            )
        with database() as connection:
            connection.execute("UPDATE shop_products SET listed = 0 WHERE shop_id = ?", (shop_id,))
            excluded = {
                row["goods_key"]
                for row in connection.execute(
                    "SELECT goods_key FROM shop_exclusions WHERE shop_id = ?", (shop_id,)
                )
            }
            imported_count = 0
            for product in products:
                if product["goods_key"] in excluded:
                    continue
                imported_count += 1
                connection.execute(
                    """
                    INSERT INTO watches(url, name, enabled, interval_seconds, created_at)
                    VALUES(?, ?, 0, ?, ?)
                    ON CONFLICT(url) DO NOTHING
                    """,
                    (product["source_url"], product["title"], shop["interval_seconds"], stamp),
                )
                watch = connection.execute(
                    "SELECT id FROM watches WHERE url = ?", (product["source_url"],)
                ).fetchone()
                watch_id = watch["id"]
                connection.execute(
                    """
                    INSERT INTO snapshots(
                        watch_id, title, price, market_price, stock, description, specs,
                        image, sale_status, goods_key, raw_data, fetched_at, status, error
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', NULL)
                    """,
                    (
                        watch_id,
                        product["title"],
                        product["price"],
                        product["market_price"],
                        None if product["stock"] in (None, "") else str(product["stock"]),
                        product["description"],
                        json.dumps(product["specs"], ensure_ascii=False),
                        product["image"],
                        product["sale_status"],
                        product["goods_key"],
                        json.dumps(product["raw_data"], ensure_ascii=False),
                        stamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen)
                    VALUES(?, ?, ?, 1, ?)
                    ON CONFLICT(shop_id, goods_key) DO UPDATE SET
                        watch_id = excluded.watch_id,
                        listed = 1,
                        last_seen = excluded.last_seen
                    """,
                    (shop_id, product["goods_key"], watch_id, stamp),
                )
            connection.execute(
                "INSERT INTO shop_runs(shop_id, fetched_at, status, product_count) VALUES(?, ?, 'success', ?)",
                (shop_id, stamp, imported_count),
            )
            detected_name = next(
                (
                    str((product["raw_data"].get("user") or {}).get("nickname") or "").strip()
                    for product in products
                    if isinstance(product.get("raw_data", {}).get("user"), dict)
                    and str((product["raw_data"].get("user") or {}).get("nickname") or "").strip()
                ),
                "",
            )
            if detected_name:
                connection.execute(
                    "UPDATE shops SET name = ? WHERE id = ? AND (name IS NULL OR name = '' OR name = token)",
                    (detected_name[:100], shop_id),
                )
            connection.execute("UPDATE shops SET last_run = ? WHERE id = ?", (stamp, shop_id))
            connection.execute(
                """
                UPDATE watches SET last_run = ? WHERE id IN (
                    SELECT watch_id FROM shop_products WHERE shop_id = ? AND listed = 1
                )
                """,
                (stamp, shop_id),
            )
            sync_shop_product_intervals(connection, shop_id, int(shop["interval_seconds"]))
        imported_products = [product for product in products if product["goods_key"] not in excluded]
        known_stock = [product["stock"] for product in imported_products if product["stock"] not in (None, "")]
        return {
            "status": "success",
            "fetched_at": stamp,
            "product_count": len(imported_products),
            "known_stock_count": len(known_stock),
            "on_sale_count": sum(product["sale_status"] == "on_sale" for product in imported_products),
        }
    except Exception as exc:
        error = str(exc)[:240] or "店铺抓取失败"
        with database() as connection:
            connection.execute(
                "INSERT INTO shop_runs(shop_id, fetched_at, status, error) VALUES(?, ?, 'error', ?)",
                (shop_id, stamp, error),
            )
            connection.execute("UPDATE shops SET last_run = ? WHERE id = ?", (stamp, shop_id))
        raise RuntimeError(error) from exc


def _watch_inventory_shop_id(watch_id: int) -> int | None:
    with database() as connection:
        watch = connection.execute("SELECT 1 FROM watches WHERE id = ?", (watch_id,)).fetchone()
        if watch is None:
            raise KeyError("监控商品不存在")
        shop = connection.execute(
            """
            SELECT s.id FROM shops s
            JOIN shop_products sp ON sp.shop_id = s.id
            WHERE sp.watch_id = ? AND sp.listed = 1
            ORDER BY s.enabled DESC, s.id
            LIMIT 1
            """,
            (watch_id,),
        ).fetchone()
    return int(shop["id"]) if shop else None


def _latest_watch_product(watch_id: int, *, shop_id: int, shop_summary: dict[str, Any]) -> dict[str, Any]:
    with database() as connection:
        linked = connection.execute(
            "SELECT 1 FROM shop_products WHERE shop_id = ? AND watch_id = ? AND listed = 1",
            (shop_id, watch_id),
        ).fetchone()
        snapshots = connection.execute(
            """
            SELECT * FROM snapshots
            WHERE watch_id = ? AND status = 'success'
            ORDER BY id DESC LIMIT 2
            """,
            (watch_id,),
        ).fetchall()
    if linked is None or not snapshots:
        raise RuntimeError("店铺同步完成，但该商品已不在店铺列表")
    product = serialize_snapshot(snapshots[0]) or {}
    previous_price = snapshots[1]["price"] if len(snapshots) > 1 else None
    product["previous_price"] = previous_price
    product["price_changed"] = (
        previous_price not in (None, "") and previous_price != product.get("price")
    )
    product["refresh_source"] = "shop"
    product["shop_id"] = shop_id
    product["shop_summary"] = shop_summary
    return product


def record_inventory_fetch(
    watch_id: int,
    shop_refresh_cache: dict[int, dict[str, Any] | Exception] | None = None,
) -> dict[str, Any]:
    shop_id = _watch_inventory_shop_id(watch_id)
    if shop_id is None:
        product = record_fetch(watch_id)
        product["refresh_source"] = "item"
        return product

    cached = shop_refresh_cache.get(shop_id) if shop_refresh_cache is not None else None
    if isinstance(cached, Exception):
        raise RuntimeError(str(cached)) from cached
    if isinstance(cached, dict):
        summary = cached
    else:
        try:
            summary = record_shop_fetch(shop_id)
        except Exception as exc:
            if shop_refresh_cache is not None:
                shop_refresh_cache[shop_id] = exc
            raise
        if shop_refresh_cache is not None:
            shop_refresh_cache[shop_id] = summary
    return _latest_watch_product(watch_id, shop_id=shop_id, shop_summary=summary)


def list_shops() -> list[dict[str, Any]]:
    with database() as connection:
        result: list[dict[str, Any]] = []
        for row in connection.execute("SELECT * FROM shops ORDER BY id DESC"):
            latest_run = connection.execute(
                "SELECT * FROM shop_runs WHERE shop_id = ? ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            product_rows = connection.execute(
                "SELECT watch_id FROM shop_products WHERE shop_id = ? AND listed = 1",
                (row["id"],),
            ).fetchall()
            known_stock_count = 0
            total_stock = 0
            on_sale_count = 0
            for product_row in product_rows:
                snapshot = connection.execute(
                    """
                    SELECT stock, sale_status FROM snapshots
                    WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1
                    """,
                    (product_row["watch_id"],),
                ).fetchone()
                if snapshot is None:
                    continue
                if snapshot["sale_status"] == "on_sale":
                    on_sale_count += 1
                if str(snapshot["stock"] or "").isdigit():
                    known_stock_count += 1
                    total_stock += int(snapshot["stock"])
            shop = dict(row)
            shop["enabled"] = bool(shop["enabled"])
            shop["last_attempt"] = dict(latest_run) if latest_run else None
            shop["product_count"] = len(product_rows)
            shop["on_sale_count"] = on_sale_count
            shop["known_stock_count"] = known_stock_count
            shop["total_stock"] = total_stock if known_stock_count else None
            result.append(shop)
        return result


def list_watches() -> list[dict[str, Any]]:
    with database() as connection:
        result: list[dict[str, Any]] = []
        for row in connection.execute("SELECT * FROM watches ORDER BY id DESC"):
            latest = connection.execute(
                "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            attempt = connection.execute(
                "SELECT fetched_at, status, error FROM snapshots WHERE watch_id = ? ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            previous = connection.execute(
                "SELECT price FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1 OFFSET 1",
                (row["id"],),
            ).fetchone()
            watch = dict(row)
            watch["enabled"] = bool(watch["enabled"])
            watch["latest"] = effective_watch_product(connection, row["id"], latest, attempt)
            watch["last_attempt"] = dict(attempt) if attempt else None
            shop_links = connection.execute(
                """
                SELECT s.id, s.name, s.token FROM shop_products sp
                JOIN shops s ON s.id = sp.shop_id
                WHERE sp.watch_id = ? AND sp.listed = 1
                """,
                (row["id"],),
            ).fetchall()
            watch["shops"] = [dict(link) for link in shop_links]
            watch["price_changed"] = bool(
                latest and previous and latest["price"] not in (None, "") and latest["price"] != previous["price"]
            )
            result.append(watch)
        return result


def checkout_settings() -> dict[str, Any]:
    return monitor_setting_store.read_checkout(database)


def _setting_json(key: str, fallback: dict[str, Any]) -> dict[str, Any]:
    return monitor_setting_store.read_json(database, key, fallback)


def _normalize_service_url(value: Any, default: str) -> str:
    return monitor_setting_store.normalize_service_url(value, default)


def redeem_settings() -> dict[str, Any]:
    value = _setting_json("redeem", {"base_url": DEFAULT_REDEEM_URL})
    return {"base_url": _normalize_service_url(value.get("base_url"), DEFAULT_REDEEM_URL)}


def sub2api_settings(*, reveal: bool = False) -> dict[str, Any]:
    return sub2api_config.read_settings(
        _setting_json,
        _normalize_service_url,
        reveal=reveal,
    )


def sub2api_automation_settings() -> dict[str, Any]:
    return sub2api_config.read_automation_settings(_setting_json)


def sub2api_automation_state() -> dict[str, Any]:
    return sub2api_config.read_automation_state(_setting_json)


def _store_sub2api_automation_state(value: dict[str, Any]) -> None:
    _store_setting("sub2api_automation_state", value)


def _store_setting(key: str, value: dict[str, Any]) -> None:
    monitor_setting_store.store_json(database, key, value)


def _external_json_request(
    method: str,
    endpoint: str,
    payload: Any = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[int, Any, str]:
    body = None
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_EXTERNAL_JSON_BYTES:
            raise ValueError("请求内容过大")
        request_headers.setdefault("Content-Type", "application/json")
    request = Request(endpoint, data=body, headers=request_headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read(MAX_EXTERNAL_JSON_BYTES + 1)
    except HTTPError as exc:
        raw = exc.read(MAX_EXTERNAL_JSON_BYTES + 1)
        status = int(exc.code)
    except URLError as exc:
        raise RuntimeError(f"无法连接上游服务：{str(exc.reason)[:120]}") from exc
    if len(raw) > MAX_EXTERNAL_JSON_BYTES:
        raise RuntimeError("上游响应过大")
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = {"raw": text[:1000]}
    return status, parsed, text


def _redeem_client() -> Any:
    try:
        from redeem_api_sdk import RedeemClient
    except Exception as exc:
        raise RuntimeError("401 找回依赖未安装，请先运行 pip install -r backend/requirements.txt") from exc
    return RedeemClient(redeem_settings()["base_url"], timeout=30)


def _dataclass_to_json(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        result = {}
        for field_name in value.__dataclass_fields__:
            result[field_name] = _dataclass_to_json(getattr(value, field_name))
        return result
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {str(key): _dataclass_to_json(item) for key, item in vars(value).items() if not key.startswith("_")}
    if isinstance(value, list):
        return [_dataclass_to_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _dataclass_to_json(item) for key, item in value.items()}
    return value


# Compatibility wrappers keep existing imports and runtime patches stable while
# the implementation lives in focused Sub2API modules.
def _validate_sub2api_data(data: Any) -> dict[str, Any]:
    return sub2api_payloads.validate_data(data)


def _merge_sub2api_data(data: Any) -> dict[str, Any]:
    return sub2api_payloads.merge_data(data)


SUB2API_CODEX_FINGERPRINT_MODES = CODEX_FINGERPRINT_MODES


def _sub2api_apply_codex_fingerprint_mode(
    normalized: dict[str, Any], raw_mode: Any
) -> tuple[dict[str, Any], str | None, int]:
    return sub2api_payloads.apply_codex_fingerprint_mode(normalized, raw_mode)


def _sub2api_payload_data(payload: Any) -> Any:
    return sub2api_payloads.payload_data(payload)


def _sub2api_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    return sub2api_payloads.payload_list(payload, *keys)


def _sub2api_upstream_error(status: int, payload: Any, raw: str) -> str:
    return sub2api_payloads.upstream_error(status, payload, raw)


def _sub2api_fetch_accounts(
    config: dict[str, Any], *, platform: str = "", account_type: str = ""
) -> list[dict[str, Any]]:
    return sub2api_client.fetch_accounts(
        config,
        request_json=_external_json_request,
        platform=platform,
        account_type=account_type,
    )


def _sub2api_codex_accounts(config: dict[str, Any]) -> list[dict[str, Any]]:
    return sub2api_client.codex_accounts(config, accounts_loader=_sub2api_fetch_accounts)


def _sub2api_created_account_ids(payload: Any) -> set[int]:
    return sub2api_payloads.created_account_ids(payload)


def _sub2api_fingerprint_targets(
    imported_accounts: list[dict[str, Any]],
    upstream_accounts: list[dict[str, Any]],
    created_ids: set[int],
) -> list[dict[str, Any]]:
    return sub2api_payloads.fingerprint_targets(imported_accounts, upstream_accounts, created_ids)


def _sub2api_reconcile_codex_fingerprint(
    config: dict[str, Any],
    normalized: dict[str, Any],
    mode: str | None,
    import_payload: Any,
) -> dict[str, Any]:
    return sub2api_client.reconcile_codex_fingerprint(
        config,
        normalized,
        mode,
        import_payload,
        request_json=_external_json_request,
        codex_accounts_loader=_sub2api_codex_accounts,
    )


def _sub2api_fingerprint_verification_error(
    mode: str | None, eligible: int, error: Exception
) -> dict[str, Any]:
    return sub2api_payloads.fingerprint_verification_error(mode, eligible, error)


SUB2API_401_TEXT_PATTERNS = sub2api_reclaim.ERROR_401_TEXT_PATTERNS


def _sub2api_error_text_is_401(value: Any) -> bool:
    return sub2api_reclaim.error_text_is_401(value)


def _sub2api_structured_error_is_401(value: Any, *, error_context: bool = False) -> bool:
    return sub2api_reclaim.structured_error_is_401(value, error_context=error_context)


def _sub2api_account_is_401(account: dict[str, Any]) -> bool:
    return sub2api_reclaim.account_is_401(account)


def _download_reclaim_payloads(
    reclaim_result: dict[str, Any], client: Any, *, exclude_order_nos: list[str] | None = None
) -> list[dict[str, Any]]:
    return sub2api_reclaim.download_payloads(
        reclaim_result,
        client,
        exclude_order_nos=exclude_order_nos,
        max_payload_bytes=MAX_EXTERNAL_JSON_BYTES,
    )


def reclaim_sub2api_401_accounts(
    *, include_downloads: bool = True, exclude_order_nos: list[str] | None = None
) -> dict[str, Any]:
    return sub2api_reclaim.reclaim_401_accounts(
        config=sub2api_settings(reveal=True),
        accounts_loader=_sub2api_fetch_accounts,
        redeem_client_factory=_redeem_client,
        to_json=_dataclass_to_json,
        max_payload_bytes=MAX_EXTERNAL_JSON_BYTES,
        include_downloads=include_downloads,
        exclude_order_nos=exclude_order_nos,
    )


def _sub2api_datetime(value: Any) -> datetime | None:
    return sub2api_payloads.datetime_value(value)


def _sub2api_monitor_summary(
    accounts: list[dict[str, Any]],
    proxies: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    return sub2api_payloads.monitor_summary(
        accounts,
        proxies,
        groups,
        now=now,
        now_iso=utc_now,
    )


def fetch_sub2api_options() -> dict[str, Any]:
    return sub2api_client.fetch_options(
        sub2api_settings(reveal=True),
        request_json=_external_json_request,
        accounts_loader=_sub2api_fetch_accounts,
        monitor_builder=_sub2api_monitor_summary,
    )


def _sub2api_assignment_payload(
    normalized: dict[str, Any], proxy_id: Any, raw_group_ids: Any
) -> tuple[dict[str, Any], int | None, list[int]]:
    return sub2api_payloads.assignment_payload(normalized, proxy_id, raw_group_ids)


def _sub2api_import_payload(
    source: Any,
    *,
    proxy_id: Any = None,
    group_ids: Any = None,
    codex_fingerprint_mode: Any = None,
    assign_existing: bool | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    return sub2api_client.import_payload(
        source,
        config=sub2api_settings(reveal=True),
        request_json=_external_json_request,
        fingerprint_reconciler=_sub2api_reconcile_codex_fingerprint,
        proxy_id=proxy_id,
        group_ids=group_ids,
        codex_fingerprint_mode=codex_fingerprint_mode,
        assign_existing=assign_existing,
        endpoint=endpoint,
    )


def test_sub2api_connection() -> dict[str, Any]:
    return sub2api_client.test_connection(
        sub2api_settings(reveal=True),
        request_json=_external_json_request,
    )


def save_sub2api_automation_settings(data: dict[str, Any]) -> dict[str, Any]:
    return sub2api_config.save_automation_settings(
        data,
        has_admin_key=lambda: bool(sub2api_settings(reveal=True)["admin_key"]),
        store_setting=_store_setting,
    )


def refresh_sub2api_reclaim(
    card_codes: list[str], *, exclude_order_nos: list[str] | None = None
) -> dict[str, Any]:
    return sub2api_reclaim.refresh_reclaim(
        card_codes,
        redeem_client_factory=_redeem_client,
        to_json=_dataclass_to_json,
        max_payload_bytes=MAX_EXTERNAL_JSON_BYTES,
        exclude_order_nos=exclude_order_nos,
    )


def run_sub2api_automation_cycle() -> dict[str, Any]:
    return sub2api_automation.run_cycle(
        settings_loader=sub2api_automation_settings,
        state_loader=sub2api_automation_state,
        refresh_reclaim=refresh_sub2api_reclaim,
        reclaim_accounts=reclaim_sub2api_401_accounts,
        import_payload=_sub2api_import_payload,
        store_state=_store_sub2api_automation_state,
        now=utc_now,
    )


def list_preorders() -> list[dict[str, Any]]:
    with database() as connection:
        rows = connection.execute("SELECT * FROM preorders ORDER BY id DESC").fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            watch = connection.execute("SELECT url, name FROM watches WHERE id = ?", (row["watch_id"],)).fetchone()
            latest = connection.execute(
                "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                (row["watch_id"],),
            ).fetchone()
            product = effective_watch_product(connection, row["watch_id"], latest)
            item = dict(row)
            item.pop("contact", None)
            item.pop("query_password", None)
            item["enabled"] = bool(item["enabled"])
            item["title"] = (product or {}).get("title") or (watch["name"] if watch else "") or f"商品 {row['watch_id']}"
            item["official_url"] = watch["url"] if watch else ""
            item["current_stock"] = (product or {}).get("stock")
            item["stock_label"] = (product or {}).get("stock_label") or "数量待获取"
            item["sale_status"] = (product or {}).get("sale_status")
            result.append(item)
    return result


def create_preorders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("enabled") is not True:
        raise ValueError("请先勾选启用自动预购")
    entries = payload.get("items")
    if not isinstance(entries, list) or not entries or len(entries) > 100:
        raise ValueError("预购清单应包含 1 到 100 个商品")
    config = checkout_settings()
    try:
        interval = normalize_interval(payload.get("interval_seconds"), 1)
        channel_id = int(config.get("channel_id", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("预购配置无效") from exc
    contact = str(config.get("contact") or "").strip()
    query_password = str(config.get("query_password") or "")
    if not contact:
        raise PreorderConflict("请先保存购买联系方式")
    if channel_id < 1 or channel_id > 99:
        raise ValueError("支付渠道配置无效")

    normalized: list[tuple[int, int]] = []
    seen: set[int] = set()
    for entry in entries:
        try:
            watch_id = int(entry.get("watch_id"))
            quantity = int(entry.get("quantity", 1))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("预购商品或数量格式无效") from exc
        if watch_id in seen:
            raise ValueError("预购清单包含重复商品")
        if quantity < 1 or quantity > 99:
            raise ValueError("单项预购数量应为 1 到 99")
        seen.add(watch_id)
        normalized.append((watch_id, quantity))

    stamp = utc_now()
    with database() as connection:
        validated: list[tuple[int, int]] = []
        for watch_id, quantity in normalized:
            watch = connection.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
            latest = connection.execute(
                "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                (watch_id,),
            ).fetchone()
            if watch is None or latest is None:
                raise PreorderConflict(f"商品 {watch_id} 尚无有效库存数据")
            product = effective_watch_product(connection, watch_id, latest)
            title = product.get("title") or watch["name"] or f"商品 {watch_id}"
            if product.get("sale_status") != "on_sale":
                raise PreorderConflict(f"{title} 当前未上架，不能启用预购")
            if product.get("stock") is None:
                raise PreorderConflict(f"{title} 的库存数量未知，不能启用预购")
            if product["stock"] != 0:
                raise PreorderConflict(f"{title} 当前不是缺货状态，不会启用预购")
            minimum = int(product.get("limit_count") or 1)
            if quantity < minimum:
                raise PreorderConflict(f"{title} 最低 {minimum} 件起购")
            if product.get("query_password_required") and not query_password:
                raise PreorderConflict(f"{title} 需要查询密码，请先保存购买配置")
            shop = connection.execute(
                """
                SELECT s.token FROM shops s JOIN shop_products sp ON sp.shop_id = s.id
                WHERE sp.watch_id = ? AND sp.listed = 1 ORDER BY s.id LIMIT 1
                """,
                (watch_id,),
            ).fetchone()
            if shop is None:
                raise PreorderConflict(f"{title} 未关联店铺，请先通过店铺链接同步")
            processing = connection.execute(
                "SELECT 1 FROM preorders WHERE watch_id = ? AND status = 'processing'",
                (watch_id,),
            ).fetchone()
            if processing:
                raise PreorderConflict(f"{title} 正在创建订单，请勿重复设置")
            validated.append((watch_id, quantity))

        for watch_id, quantity in validated:
            connection.execute(
                """
                INSERT INTO preorders(
                    watch_id, quantity, interval_seconds, enabled, status, contact,
                    query_password, channel_id, last_check, last_error, trade_no,
                    payment_url, amount, created_at, triggered_at
                ) VALUES(?, ?, ?, 1, 'watching', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, NULL)
                ON CONFLICT(watch_id) DO UPDATE SET
                    quantity = excluded.quantity,
                    interval_seconds = excluded.interval_seconds,
                    enabled = 1,
                    status = 'watching',
                    contact = excluded.contact,
                    query_password = excluded.query_password,
                    channel_id = excluded.channel_id,
                    last_check = NULL,
                    last_error = NULL,
                    trade_no = NULL,
                    payment_url = NULL,
                    amount = NULL,
                    created_at = excluded.created_at,
                    triggered_at = NULL
                """,
                (watch_id, quantity, interval, contact, query_password, channel_id, stamp),
            )
    return list_preorders()


def process_preorder(preorder_id: int, product: dict[str, Any]) -> dict[str, Any] | None:
    stamp = utc_now()
    with database() as connection:
        preorder = connection.execute(
            "SELECT * FROM preorders WHERE id = ? AND enabled = 1 AND status = 'watching'",
            (preorder_id,),
        ).fetchone()
        if preorder is None:
            return None
        connection.execute(
            "UPDATE preorders SET last_check = ?, last_error = NULL WHERE id = ?",
            (stamp, preorder_id),
        )
        stock = product.get("stock")
        if product.get("sale_status") != "on_sale" or stock is None or int(stock) < int(preorder["quantity"]):
            return None
        claimed = connection.execute(
            "UPDATE preorders SET status = 'processing' WHERE id = ? AND status = 'watching' AND enabled = 1",
            (preorder_id,),
        )
        if claimed.rowcount != 1:
            return None
        watch = connection.execute("SELECT url FROM watches WHERE id = ?", (preorder["watch_id"],)).fetchone()
        shop = connection.execute(
            """
            SELECT s.token FROM shops s JOIN shop_products sp ON sp.shop_id = s.id
            WHERE sp.watch_id = ? AND sp.listed = 1 ORDER BY s.id LIMIT 1
            """,
            (preorder["watch_id"],),
        ).fetchone()

    try:
        if watch is None or shop is None:
            raise RuntimeError("商品店铺关联已失效")
        goods_key = str(product.get("goods_key") or "").strip()
        if not goods_key:
            raise RuntimeError("商品编号不可用")
        identity = fetch_buyer_juuid(shop["token"])
        order = create_official_payment_order(
            goods_key=goods_key,
            quantity=int(preorder["quantity"]),
            coupon_code="",
            channel_id=int(preorder["channel_id"]),
            contact=preorder["contact"],
            query_password=preorder["query_password"] if product.get("query_password_required") else "",
            select_cards_ids=[],
            juuid=identity["juuid"],
            referer=watch["url"],
        )
    except Exception as exc:
        with database() as connection:
            connection.execute(
                "UPDATE preorders SET enabled = 0, status = 'error', last_error = ? WHERE id = ? AND status = 'processing'",
                (str(exc)[:240] or "创建订单失败", preorder_id),
            )
        return {"status": "error", "detail": str(exc)}

    with database() as connection:
        connection.execute(
            """
            UPDATE preorders SET enabled = 0, status = 'triggered', trade_no = ?,
                payment_url = ?, amount = ?, triggered_at = ?, last_error = NULL
            WHERE id = ? AND status = 'processing'
            """,
            (order.get("trade_no"), order.get("payment_url"), order.get("amount"), utc_now(), preorder_id),
        )
    return {"status": "triggered", "order": order}


def mark_preorder_check_error(preorder_id: int, error: Exception) -> None:
    with database() as connection:
        connection.execute(
            "UPDATE preorders SET last_check = ?, last_error = ? WHERE id = ? AND status = 'watching'",
            (utc_now(), str(error)[:240] or "库存检查失败", preorder_id),
        )


def price_history(
    watch_id: int,
    *,
    limit: int,
    offset: int = 0,
    start_date: str = "",
    end_date: str = "",
    status: str = "all",
    stock: str = "all",
    query: str = "",
) -> dict[str, Any]:
    return monitor_history.price_history(
        database,
        _money,
        watch_id,
        limit=limit,
        offset=offset,
        start_date=start_date,
        end_date=end_date,
        status=status,
        stock=stock,
        query=query,
        missing_message="监控商品不存在",
    )


def delete_watches(watch_ids: list[int]) -> int:
    if not watch_ids:
        return 0
    placeholders = ",".join("?" for _ in watch_ids)
    with database() as connection:
        linked_products = connection.execute(
            f"""
            SELECT shop_id, goods_key FROM shop_products
            WHERE watch_id IN ({placeholders})
            """,
            watch_ids,
        ).fetchall()
        for link in linked_products:
            connection.execute(
                """
                INSERT INTO shop_exclusions(shop_id, goods_key, removed_at)
                VALUES(?, ?, ?)
                ON CONFLICT(shop_id, goods_key) DO UPDATE SET removed_at = excluded.removed_at
                """,
                (link["shop_id"], link["goods_key"], utc_now()),
            )
        cursor = connection.execute(
            f"DELETE FROM watches WHERE id IN ({placeholders})",
            watch_ids,
        )
    return cursor.rowcount


class MonitorWorker(CoreMonitorWorker):
    def __init__(self) -> None:
        # Lambdas resolve main-module names when work runs, preserving runtime
        # overrides used by the HTTP layer and the existing test suite.
        super().__init__(
            database=lambda: database(),
            record_inventory_fetch=lambda watch_id, cache=None: record_inventory_fetch(watch_id, cache),
            record_shop_fetch=lambda shop_id: record_shop_fetch(shop_id),
            process_preorder=lambda preorder_id, product: process_preorder(preorder_id, product),
            mark_preorder_check_error=lambda preorder_id, error: mark_preorder_check_error(preorder_id, error),
            default_interval=DEFAULT_INTERVAL,
        )


WORKER = MonitorWorker()


class Sub2ApiAutomationWorker(sub2api_worker.Sub2ApiAutomationWorker):
    def __init__(self) -> None:
        super().__init__(
            run_cycle=lambda: run_sub2api_automation_cycle(),
            settings_loader=lambda: sub2api_automation_settings(),
            state_loader=lambda: sub2api_automation_state(),
            store_state=lambda value: _store_sub2api_automation_state(value),
            now=lambda: utc_now(),
        )


AUTOMATION_WORKER = Sub2ApiAutomationWorker()


class BrowserVerificationManager(CoreBrowserVerificationManager):
    def __init__(self) -> None:
        super().__init__(
            database=lambda: database(),
            worker_lock=WORKER.fetch_lock,
            record_shop_fetch=lambda shop_id, **kwargs: record_shop_fetch(shop_id, **kwargs),
            goods_list_rows=lambda payload: _goods_list_rows(payload),
            normalize_goods=lambda item, token: normalize_goods_list_item(item, token),
            first_value=lambda item, keys: _first_value(item, keys),
            waf_error=WafChallengeRequired,
            waf_markers=WAF_MARKERS,
            profile_path=Path(__file__).with_name("waf-browser-profile"),
        )


BROWSER_VERIFICATION = BrowserVerificationManager()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "LDXPLocalMonitor/2.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin", "")
        if re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d{2,5}", origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求长度无效") from exc
        if length > MAX_REQUEST_JSON_BYTES:
            raise ValueError("请求内容过大")
        try:
            parsed = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(parsed, dict):
                raise ValueError("请求 JSON 必须是对象")
            return parsed
        except json.JSONDecodeError as exc:
            raise ValueError("请求 JSON 无效") from exc

    def do_OPTIONS(self) -> None:
        self._send_json({"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            return self._send_redirect(FRONTEND_URL)
        if path == "/api/health":
            return self._send_json({
                "ok": True,
                "time": utc_now(),
                "worker": WORKER.is_alive(),
                "sub2api_automation_worker": AUTOMATION_WORKER.is_alive(),
            })
        if path == "/api/watches":
            return self._send_json(list_watches())
        if path == "/api/shops":
            return self._send_json(list_shops())
        if path == "/api/preorders":
            return self._send_json(list_preorders())
        shop_products_match = re.fullmatch(r"/api/shops/(\d+)/products", path)
        if shop_products_match:
            shop_id = int(shop_products_match.group(1))
            shops = {shop["id"] for shop in list_shops()}
            if shop_id not in shops:
                return self._send_json({"detail": "监控店铺不存在"}, 404)
            products = [
                watch for watch in list_watches()
                if any(link["id"] == shop_id for link in watch.get("shops", []))
            ]
            return self._send_json(products)
        match = re.fullmatch(r"/api/watches/(\d+)/history", path)
        if match:
            query_values = parse_qs(parsed.query)
            def query_value(name: str, default: str = "") -> str:
                return str(query_values.get(name, [default])[0] or default)

            try:
                limit = int(query_value("limit", "25"))
                offset = int(query_value("offset", "0"))
            except ValueError:
                return self._send_json({"detail": "分页参数无效"}, 400)
            try:
                return self._send_json(price_history(
                    int(match.group(1)),
                    limit=limit,
                    offset=offset,
                    start_date=query_value("start_date"),
                    end_date=query_value("end_date"),
                    status=query_value("status", "all"),
                    stock=query_value("stock", "all"),
                    query=query_value("query"),
                ))
            except KeyError as exc:
                return self._send_json({"detail": str(exc.args[0])}, 404)
        if path == "/api/settings/contact":
            with database() as connection:
                row = connection.execute("SELECT value FROM settings WHERE key = 'contact'").fetchone()
            return self._send_json(_json_value(row["value"] if row else None, {"contact": "", "note": ""}))
        if path == "/api/settings/checkout":
            return self._send_json(checkout_settings())
        if path == "/api/redeem/config":
            return self._send_json(redeem_settings())
        if sub2api_routes.handle_get(
            path,
            send_json=self._send_json,
            settings_loader=sub2api_settings,
            automation_settings_loader=sub2api_automation_settings,
            automation_state_loader=sub2api_automation_state,
            options_loader=fetch_sub2api_options,
        ):
            return
        if path == "/api/pay/juuid":
            token = parse_qs(parsed.query).get("token", [""])[0]
            try:
                return self._send_json(fetch_buyer_juuid(token))
            except (TypeError, ValueError) as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)
        if path == "/api/pay/channels":
            token = parse_qs(parsed.query).get("token", [""])[0]
            try:
                return self._send_json(fetch_payment_channels(token))
            except (TypeError, ValueError) as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)
        return self._send_json({"detail": "接口不存在"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        try:
            data = self._read_json()
        except ValueError as exc:
            return self._send_json({"detail": str(exc)}, 400)

        if path == "/api/preorders":
            try:
                result = create_preorders(data)
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except PreorderConflict as exc:
                return self._send_json({"detail": str(exc)}, 409)
            return self._send_json(result, 201)

        if path in ("/api/redeem/health-check", "/api/redeem/reclaim", "/api/redeem/progress"):
            raw_codes = data.get("card_codes")
            if isinstance(raw_codes, str):
                raw_codes = re.split(r"[\s,]+", raw_codes)
            if not isinstance(raw_codes, list):
                return self._send_json({"detail": "请提供 card_codes 数组"}, 400)
            card_codes = [str(code).strip() for code in raw_codes if str(code).strip()]
            if not card_codes or len(card_codes) > 100:
                return self._send_json({"detail": "卡密数量应为 1 到 100 个"}, 400)
            try:
                client = _redeem_client()
                if path.endswith("health-check"):
                    result = client.health_check(card_codes)
                elif path.endswith("/reclaim"):
                    mode = str(data.get("mode") or "401")
                    if mode not in ("401", "all"):
                        return self._send_json({"detail": "找回模式无效"}, 400)
                    result = client.batch_reclaim(card_codes, mode=mode)
                else:
                    result = client.refresh_progress(card_codes)
                payload = _dataclass_to_json(result)
                return self._send_json(payload, 200 if payload.get("ok", False) else 502)
            except (TypeError, ValueError) as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except Exception as exc:
                return self._send_json({"detail": str(exc)[:240]}, 502)

        if path == "/api/redeem/download":
            order_no = str(data.get("order_no") or "").strip()
            token = str(data.get("download_token") or data.get("token") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,160}", order_no) or not token or len(token) > 512:
                return self._send_json({"detail": "下载参数无效"}, 400)
            try:
                content = _redeem_client().download(order_no, token)
            except Exception as exc:
                return self._send_json({"detail": str(exc)[:240]}, 502)
            if not content:
                return self._send_json({"detail": "找回文件尚未可下载或令牌已失效"}, 404)
            try:
                parsed = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            return self._send_json({
                "filename": f"{order_no}.json",
                "content_base64": base64.b64encode(content).decode("ascii"),
                "data": parsed,
            })

        if sub2api_routes.handle_post(
            path,
            data,
            send_json=self._send_json,
            test_connection=test_sub2api_connection,
            reclaim_accounts=reclaim_sub2api_401_accounts,
            refresh_reclaim=refresh_sub2api_reclaim,
            run_automation=AUTOMATION_WORKER.run_once,
            import_payload=_sub2api_import_payload,
        ):
            return

        if path == "/api/watches/batch-delete":
            raw_ids = data.get("ids")
            if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 500:
                return self._send_json({"detail": "请选择 1 到 500 个商品"}, 400)
            try:
                watch_ids = sorted({int(value) for value in raw_ids})
            except (TypeError, ValueError):
                return self._send_json({"detail": "商品编号无效"}, 400)
            if any(value < 1 for value in watch_ids):
                return self._send_json({"detail": "商品编号无效"}, 400)
            deleted_count = delete_watches(watch_ids)
            return self._send_json({"ok": True, "deleted_count": deleted_count})

        if path == "/api/watches":
            try:
                _, canonical_url = parse_item_url(str(data.get("url") or ""))
                interval = normalize_interval(data.get("interval_seconds"))
                name = str(data.get("name") or "").strip()[:100]
                with database() as connection:
                    cursor = connection.execute(
                        "INSERT INTO watches(url, name, enabled, interval_seconds, created_at) VALUES(?, ?, 1, ?, ?)",
                        (canonical_url, name, interval, utc_now()),
                    )
                    watch_id = cursor.lastrowid
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except sqlite3.IntegrityError:
                return self._send_json({"detail": "该商品已在监控列表中"}, 409)
            try:
                snapshot = WORKER.fetch(watch_id)
            except RuntimeError as exc:
                snapshot = {"status": "error", "error": str(exc)}
            return self._send_json({"id": watch_id, "snapshot": snapshot}, 201)

        if path == "/api/shops":
            try:
                token, canonical_url = parse_shop_url(str(data.get("url") or ""))
                interval = normalize_interval(data.get("interval_seconds"), DEFAULT_SHOP_INTERVAL)
                requested_name = str(data.get("name") or "").strip()[:100]
                name = requested_name or token
                keywords = str(data.get("keywords") or "").strip()[:100]
                raw_category = data.get("category_id")
                category_id = int(raw_category) if raw_category not in (None, "") else None
                goods_type = str(data.get("goods_type") or "card").strip()[:30]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", goods_type):
                    raise ValueError("商品类型格式无效")
                with database() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO shops(
                            url, token, name, keywords, category_id, goods_type,
                            enabled, interval_seconds, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (canonical_url, token, name, keywords, category_id, goods_type, interval, utc_now()),
                    )
                    shop_id = cursor.lastrowid
            except (TypeError, ValueError) as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except sqlite3.IntegrityError:
                with database() as connection:
                    existing = connection.execute(
                        "SELECT id, name FROM shops WHERE token = ? OR url = ?",
                        (token, canonical_url),
                    ).fetchone()
                    if existing is None:
                        return self._send_json({"detail": "店铺配置冲突"}, 409)
                    shop_id = existing["id"]
                    connection.execute(
                        """
                        UPDATE shops SET name = ?, keywords = ?, category_id = ?, goods_type = ?,
                            enabled = 1, interval_seconds = ? WHERE id = ?
                        """,
                        (
                            requested_name or existing["name"] or token,
                            keywords,
                            category_id,
                            goods_type,
                            interval,
                            shop_id,
                        ),
                    )
            try:
                summary = WORKER.fetch_shop(shop_id)
            except RuntimeError as exc:
                summary = {"status": "error", "error": str(exc)}
            return self._send_json({"id": shop_id, "summary": summary}, 201)

        shop_fetch_match = re.fullmatch(r"/api/shops/(\d+)/fetch", path)
        if shop_fetch_match:
            try:
                return self._send_json(WORKER.fetch_shop(int(shop_fetch_match.group(1))))
            except KeyError as exc:
                return self._send_json({"detail": str(exc.args[0])}, 404)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)

        browser_start_match = re.fullmatch(r"/api/shops/(\d+)/browser-verification/start", path)
        if browser_start_match:
            try:
                result = BROWSER_VERIFICATION.start(int(browser_start_match.group(1)))
                return self._send_json(result, 202 if result["status"] == "awaiting_verification" else 200)
            except KeyError as exc:
                return self._send_json({"detail": str(exc.args[0])}, 404)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)

        browser_complete_match = re.fullmatch(r"/api/shops/(\d+)/browser-verification/complete", path)
        if browser_complete_match:
            try:
                result = BROWSER_VERIFICATION.complete(int(browser_complete_match.group(1)))
                return self._send_json(result, 202 if result["status"] == "awaiting_verification" else 200)
            except KeyError as exc:
                return self._send_json({"detail": str(exc.args[0])}, 404)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)

        if path == "/api/shops/fetch-all":
            results = []
            for shop in list_shops():
                if not shop["enabled"]:
                    continue
                try:
                    results.append({"id": shop["id"], "ok": True, "data": WORKER.fetch_shop(shop["id"])})
                except Exception as exc:
                    results.append({"id": shop["id"], "ok": False, "error": str(exc)})
            return self._send_json({"results": results})

        if path == "/api/watches/inventory-refresh":
            raw_ids = data.get("ids")
            if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 100:
                return self._send_json({"detail": "请选择 1 到 100 个商品刷新库存"}, 400)
            try:
                watch_ids = list(dict.fromkeys(int(value) for value in raw_ids))
            except (TypeError, ValueError):
                return self._send_json({"detail": "商品编号无效"}, 400)
            if any(value < 1 for value in watch_ids):
                return self._send_json({"detail": "商品编号无效"}, 400)
            return self._send_json({"results": WORKER.fetch_many(watch_ids)})

        fetch_match = re.fullmatch(r"/api/watches/(\d+)/fetch", path)
        if fetch_match:
            try:
                return self._send_json(WORKER.fetch(int(fetch_match.group(1))))
            except KeyError as exc:
                return self._send_json({"detail": str(exc.args[0])}, 404)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)

        if path == "/api/watches/fetch-all":
            watch_ids = [watch["id"] for watch in list_watches() if watch["enabled"]]
            return self._send_json({"results": WORKER.fetch_many(watch_ids) if watch_ids else []})

        if path == "/api/checkout/prepare":
            cart = data.get("items")
            if not isinstance(cart, list) or not cart or len(cart) > 20:
                return self._send_json({"detail": "购买清单应包含 1 到 20 个商品"}, 400)
            prepared = []
            total = Decimal("0")
            with database() as connection:
                for entry in cart:
                    try:
                        watch_id = int(entry.get("watch_id"))
                        quantity = int(entry.get("quantity", 1))
                    except (AttributeError, TypeError, ValueError):
                        return self._send_json({"detail": "购买清单格式无效"}, 400)
                    if quantity < 1 or quantity > 99:
                        return self._send_json({"detail": "单项购买数量应为 1 到 99"}, 400)
                    watch = connection.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
                    latest = connection.execute(
                        "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                        (watch_id,),
                    ).fetchone()
                    if watch is None or latest is None:
                        return self._send_json({"detail": f"商品 {watch_id} 尚无有效抓取结果"}, 409)
                    product = effective_watch_product(connection, watch_id, latest)
                    if product["sale_status"] != "on_sale":
                        return self._send_json({"detail": f"{product['title']} 当前未上架"}, 409)
                    limit_count = product.get("limit_count")
                    if limit_count and quantity < limit_count:
                        return self._send_json({"detail": f"{product['title']} 最低 {limit_count} 件起购"}, 409)
                    try:
                        subtotal = Decimal(product["price"]) * quantity
                    except (InvalidOperation, TypeError):
                        return self._send_json({"detail": f"{product['title']} 缺少有效价格"}, 409)
                    total += subtotal
                    shop_row = connection.execute(
                        """
                        SELECT s.token FROM shops s
                        JOIN shop_products sp ON sp.shop_id = s.id
                        WHERE sp.watch_id = ? AND sp.listed = 1
                        ORDER BY s.id LIMIT 1
                        """,
                        (watch_id,),
                    ).fetchone()
                    prepared.append(
                        {
                            "watch_id": watch_id,
                            "goods_key": product.get("goods_key") or str(watch_id),
                            "title": product["title"],
                            "quantity": quantity,
                            "unit_price": product["price"],
                            "subtotal": f"{subtotal:.2f}",
                            "official_url": watch["url"],
                            "shop_token": shop_row["token"] if shop_row else "",
                            "query_password_required": product["query_password_required"],
                        }
                    )
            return self._send_json(
                {
                    "items": prepared,
                    "total": f"{total:.2f}",
                    "mode": "official_handoff",
                    "notice": "清单已校验。请在链动小铺官方页面核对并确认支付，本工具不会自动扣款。",
                }
            )

        if path == "/api/pay/order":
            items = data.get("items")
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                return self._send_json({"detail": "官方支付一次仅支持一个商品，请分开创建订单"}, 400)
            item = items[0]
            try:
                goods_key = str(item.get("goods_key") or "").strip()
                quantity = int(item.get("quantity", 1))
                channel_id = int(data.get("channel_id", 1))
                with database() as connection:
                    latest = connection.execute(
                        "SELECT * FROM snapshots WHERE goods_key = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                        (goods_key,),
                    ).fetchone()
                    product = effective_watch_product(connection, latest["watch_id"], latest) if latest else None
                if latest is None:
                    raise ValueError("商品尚未完成有效同步")
                if product["sale_status"] != "on_sale":
                    raise ValueError("商品当前未上架")
                if product.get("limit_count") and quantity < product["limit_count"]:
                    raise ValueError(f"最低 {product['limit_count']} 件起购")
                result = create_official_payment_order(
                    goods_key=goods_key,
                    quantity=quantity,
                    coupon_code=str(data.get("coupon_code") or ""),
                    channel_id=channel_id,
                    contact=str(data.get("contact") or ""),
                    query_password=str(data.get("query_password") or ""),
                    select_cards_ids=data.get("select_cards_ids"),
                    juuid=str(data.get("juuid") or ""),
                    referer=str(data.get("referer") or f"https://{ALLOWED_HOST}/item/{goods_key}"),
                    visitor_id=str(data.get("visitor_id") or ""),
                )
            except (TypeError, ValueError) as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)
            return self._send_json(result)

        if path == "/api/mock/pay/order":
            items = data.get("items")
            if not isinstance(items, list) or not items or len(items) > 20:
                return self._send_json({"detail": "模拟订单至少需要 1 个商品，最多 20 个"}, 400)
            try:
                channel_id = int(data.get("channel_id", 1))
            except (TypeError, ValueError):
                return self._send_json({"detail": "支付渠道无效"}, 400)
            if channel_id not in (1, 2):
                return self._send_json({"detail": "模拟环境仅支持支付宝(1)或微信(2)"}, 400)
            contact = str(data.get("contact") or "").strip()
            if not contact:
                return self._send_json({"detail": "请填写测试联系方式"}, 400)
            normalized_items = []
            total = Decimal("0")
            for entry in items:
                if not isinstance(entry, dict):
                    return self._send_json({"detail": "模拟商品参数无效"}, 400)
                goods_key = str(entry.get("goods_key") or "").strip()
                try:
                    quantity = int(entry.get("quantity", 1))
                    unit_price = Decimal(str(entry.get("unit_price", "0")))
                except (TypeError, ValueError, InvalidOperation):
                    return self._send_json({"detail": "模拟商品价格或数量无效"}, 400)
                if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", goods_key) or quantity < 1 or quantity > 99:
                    return self._send_json({"detail": "模拟商品编号或数量无效"}, 400)
                if unit_price < 0:
                    return self._send_json({"detail": "模拟商品价格无效"}, 400)
                subtotal = unit_price * quantity
                total += subtotal
                normalized_items.append({"goods_key": goods_key, "quantity": quantity, "unit_price": f"{unit_price:.2f}"})
            order_id = f"MOCK-{uuid.uuid4().hex[:12].upper()}"
            channel = "alipay" if channel_id == 1 else "wechat"
            payment_url = f"http://127.0.0.1:5173/mock-pay/{order_id}?channel={channel}"
            return self._send_json(
                {
                    "mode": "mock",
                    "status": "created",
                    "order_id": order_id,
                    "channel_id": channel_id,
                    "channel": channel,
                    "amount": f"{total:.2f}",
                    "payment_url": payment_url,
                    "expires_in": 900,
                    "request_preview": {
                        "goods": normalized_items,
                        "contact": contact[:3] + "***" if len(contact) > 3 else "***",
                        "query_password": "***",
                        "extend": {"juuid": "mock-juuid"},
                    },
                    "notice": "这是本地模拟订单，不会创建真实订单或发起真实支付。",
                }
            )

        return self._send_json({"detail": "接口不存在"}, 404)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        try:
            data = self._read_json()
        except ValueError as exc:
            return self._send_json({"detail": str(exc)}, 400)

        match = re.fullmatch(r"/api/watches/(\d+)", path)
        if match:
            watch_id = int(match.group(1))
            try:
                with database() as connection:
                    result = update_watch_monitoring(connection, watch_id, data)
            except (TypeError, ValueError):
                return self._send_json({"detail": "监控设置无效"}, 400)
            except MonitorNotFound as exc:
                return self._send_json({"detail": str(exc)}, 404)
            return self._send_json(result)

        shop_match = re.fullmatch(r"/api/shops/(\d+)", path)
        if shop_match:
            shop_id = int(shop_match.group(1))
            try:
                with database() as connection:
                    result = update_shop_monitoring(connection, shop_id, data)
            except (TypeError, ValueError):
                return self._send_json({"detail": "店铺监控设置无效"}, 400)
            except MonitorNotFound as exc:
                return self._send_json({"detail": str(exc)}, 404)
            return self._send_json(result)

        if path == "/api/settings/contact":
            contact = str(data.get("contact") or "").strip()[:160]
            note = str(data.get("note") or "").strip()[:160]
            value = {"contact": contact, "note": note}
            with database() as connection:
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES('contact', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (json.dumps(value, ensure_ascii=False),),
                )
            return self._send_json(value)

        if path == "/api/settings/checkout":
            contact = str(data.get("contact") or "").strip()[:160]
            note = str(data.get("note") or "").strip()[:160]
            query_password = str(data.get("query_password") or "")[:160]
            try:
                channel_id = int(data.get("channel_id", 1))
            except (TypeError, ValueError):
                return self._send_json({"detail": "支付渠道配置无效"}, 400)
            if channel_id < 1 or channel_id > 99:
                return self._send_json({"detail": "支付渠道配置无效"}, 400)
            value = {
                "contact": contact,
                "note": note,
                "query_password": query_password,
                "channel_id": channel_id,
            }
            with database() as connection:
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES('checkout', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (json.dumps(value, ensure_ascii=False),),
                )
            return self._send_json(value)

        if path == "/api/redeem/config":
            try:
                base_url = _normalize_service_url(data.get("base_url"), DEFAULT_REDEEM_URL)
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            _store_setting("redeem", {"base_url": base_url})
            return self._send_json({"base_url": base_url})

        if sub2api_routes.handle_put(
            path,
            data,
            send_json=self._send_json,
            normalize_url=_normalize_service_url,
            store_setting=_store_setting,
            settings_loader=sub2api_settings,
            save_automation_settings=save_sub2api_automation_settings,
            automation_state_loader=sub2api_automation_state,
        ):
            return

        return self._send_json({"detail": "接口不存在"}, 404)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        preorder_match = re.fullmatch(r"/api/preorders/(\d+)", path)
        if preorder_match:
            with database() as connection:
                cursor = connection.execute(
                    """
                    UPDATE preorders SET enabled = 0, status = 'cancelled'
                    WHERE id = ? AND status IN ('watching', 'error')
                    """,
                    (int(preorder_match.group(1)),),
                )
            if cursor.rowcount == 0:
                return self._send_json({"detail": "预购不存在或已结束"}, 404)
            return self._send_json({"ok": True})
        shop_match = re.fullmatch(r"/api/shops/(\d+)", path)
        if shop_match:
            with database() as connection:
                cursor = connection.execute("DELETE FROM shops WHERE id = ?", (int(shop_match.group(1)),))
            if cursor.rowcount == 0:
                return self._send_json({"detail": "监控店铺不存在"}, 404)
            return self._send_json({"ok": True})
        match = re.fullmatch(r"/api/watches/(\d+)", path)
        if not match:
            return self._send_json({"detail": "接口不存在"}, 404)
        deleted_count = delete_watches([int(match.group(1))])
        if deleted_count == 0:
            return self._send_json({"detail": "监控商品不存在"}, 404)
        return self._send_json({"ok": True})


def run() -> None:
    init_database()
    if not WORKER.is_alive():
        WORKER.start()
    if not AUTOMATION_WORKER.is_alive():
        AUTOMATION_WORKER.start()
    server = ThreadingHTTPServer((HOST, PORT), ApiHandler)
    print(f"LDXP backend running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WORKER.stop_event.set()
        AUTOMATION_WORKER.stop_event.set()
        BROWSER_VERIFICATION._close()
        server.server_close()


if __name__ == "__main__":
    run()
