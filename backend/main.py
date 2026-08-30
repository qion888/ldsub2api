from __future__ import annotations

import html
import base64
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


HOST = os.environ.get("LDXP_HOST", "127.0.0.1")
PORT = int(os.environ.get("LDXP_PORT", "8000"))
FRONTEND_URL = os.environ.get("LDXP_FRONTEND_URL", "http://127.0.0.1:5173/")
DB_PATH = Path(os.environ.get("LDXP_DB_PATH", str(Path(__file__).with_name("monitor.db"))))
ALLOWED_HOST = "pay.ldxp.cn"
DEFAULT_INTERVAL = 60
MIN_INTERVAL = 1
MAX_INTERVAL = 86400
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = "LDXP-Local-Monitor/2.0"
VISITOR_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{6,80}")
WAF_MARKERS = (b"aliyunCaptcha", b"aliyunCaptcha-sliding-slider", b"waf_nc", b"u_atoken")
DEFAULT_REDEEM_URL = "https://30d.team"
DEFAULT_SUB2API_URL = "http://127.0.0.1:8080"
DEFAULT_SUB2API_AUTOMATION = {
    "enabled": False,
    "interval_seconds": 300,
    "auto_import": False,
    "proxy_id": None,
    "group_ids": [],
    "codex_fingerprint_mode": "off",
}
MAX_EXTERNAL_JSON_BYTES = 8 * 1024 * 1024
MAX_REQUEST_JSON_BYTES = 32 * 1024 * 1024


class WafChallengeRequired(RuntimeError):
    pass


class PreorderConflict(RuntimeError):
    pass


def normalize_interval(value: Any, default: int = DEFAULT_INTERVAL) -> int:
    return min(max(int(value or default), MIN_INTERVAL), MAX_INTERVAL)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_database() -> None:
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                name TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                watch_id INTEGER NOT NULL,
                title TEXT,
                price TEXT,
                stock TEXT,
                description TEXT,
                specs TEXT,
                fetched_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                token TEXT UNIQUE NOT NULL,
                name TEXT,
                keywords TEXT NOT NULL DEFAULT '',
                category_id INTEGER,
                goods_type TEXT NOT NULL DEFAULT 'card',
                enabled INTEGER NOT NULL DEFAULT 1,
                interval_seconds INTEGER NOT NULL DEFAULT 300,
                last_run TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shop_runs (
                id INTEGER PRIMARY KEY,
                shop_id INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                status TEXT NOT NULL,
                product_count INTEGER,
                error TEXT,
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS shop_products (
                shop_id INTEGER NOT NULL,
                goods_key TEXT NOT NULL,
                watch_id INTEGER NOT NULL,
                listed INTEGER NOT NULL DEFAULT 1,
                last_seen TEXT NOT NULL,
                PRIMARY KEY (shop_id, goods_key),
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS shop_exclusions (
                shop_id INTEGER NOT NULL,
                goods_key TEXT NOT NULL,
                removed_at TEXT NOT NULL,
                PRIMARY KEY (shop_id, goods_key),
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS preorders (
                id INTEGER PRIMARY KEY,
                watch_id INTEGER NOT NULL UNIQUE,
                quantity INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'watching',
                contact TEXT NOT NULL,
                query_password TEXT NOT NULL DEFAULT '',
                channel_id INTEGER NOT NULL DEFAULT 1,
                last_check TEXT,
                last_error TEXT,
                trade_no TEXT,
                payment_url TEXT,
                amount TEXT,
                created_at TEXT NOT NULL,
                triggered_at TEXT,
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_watch_time
                ON snapshots(watch_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_shop_runs_shop_time
                ON shop_runs(shop_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_preorders_status_check
                ON preorders(status, enabled, last_check);
            """
        )
        _add_column(connection, "watches", f"interval_seconds INTEGER NOT NULL DEFAULT {DEFAULT_INTERVAL}")
        _add_column(connection, "watches", "created_at TEXT")
        _add_column(connection, "snapshots", "market_price TEXT")
        _add_column(connection, "snapshots", "image TEXT")
        _add_column(connection, "snapshots", "sale_status TEXT")
        _add_column(connection, "snapshots", "goods_key TEXT")
        _add_column(connection, "snapshots", "raw_data TEXT")
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('contact', ?)",
            (json.dumps({"contact": "", "note": ""}, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('redeem', ?)",
            (json.dumps({"base_url": DEFAULT_REDEEM_URL}, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('sub2api', ?)",
            (json.dumps({"base_url": DEFAULT_SUB2API_URL, "admin_key": ""}, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('sub2api_automation', ?)",
            (json.dumps(DEFAULT_SUB2API_AUTOMATION, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('sub2api_automation_state', ?)",
            (json.dumps({"last_run": None, "last_error": "", "last_result": None, "pending_card_codes": []}, ensure_ascii=False),),
        )
        connection.execute(
            "UPDATE watches SET created_at = COALESCE(created_at, ?) WHERE created_at IS NULL",
            (utc_now(),),
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


def normalize_goods_payload(payload: dict[str, Any], goods_key: str) -> dict[str, Any]:
    if payload.get("code") != 1 or not isinstance(payload.get("data"), dict):
        message = str(payload.get("msg") or "商品接口未返回有效数据")
        raise RuntimeError(message[:160])

    item = payload["data"]
    extend = item.get("extend") if isinstance(item.get("extend"), dict) else {}
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    seller = item.get("user") if isinstance(item.get("user"), dict) else {}
    stock_value = _stock_value(item)
    limit_count = extend.get("limit_count")
    sale_status = "on_sale" if item.get("status") == 1 else "off_sale"
    specs = {
        "商品编号": goods_key,
        "商品类型": item.get("goods_type") or "未知",
        "商品分类": category.get("name") or "未分类",
        "最低起购": limit_count if limit_count not in (None, "", 0) else 1,
        "联系方式": item.get("contact_format") or "任意",
        "查询密码": "需要" if extend.get("query_password_status") == 1 else "不需要",
        "店铺": seller.get("nickname") or "链动小铺",
    }
    return {
        "goods_key": goods_key,
        "title": str(item.get("name") or f"链动小铺商品 {goods_key}"),
        "price": _money(item.get("real_price") if item.get("real_price") not in (None, "") else item.get("price")),
        "market_price": _money(item.get("market_price")),
        "stock": stock_value,
        "stock_label": str(stock_value) if stock_value is not None else "接口未公开数量",
        "sale_status": sale_status,
        "description": plain_text(item.get("description")),
        "image": str(item.get("image") or ""),
        "specs": specs,
        "limit_count": int(limit_count) if str(limit_count or "").isdigit() else None,
        "contact_format": str(item.get("contact_format") or "any"),
        "query_password_required": extend.get("query_password_status") == 1,
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
    stock_value = _stock_value(item)
    limit_count = _first_value(extend, ("limit_count", "limit"))
    sales = _first_value(item, ("sales", "sales_count", "sold", "sale_count"))
    status_value = _first_value(item, ("status", "goods_status", "is_sale"))
    sale_status = "on_sale" if status_value is None or str(status_value).lower() in {"1", "true", "on_sale"} else "off_sale"
    specs = {
        "商品编号": goods_key,
        "商品类型": item.get("goods_type") or "未知",
        "商品分类": category.get("name") or item.get("category_name") or "未分类",
        "最低起购": limit_count if limit_count not in (None, "", 0) else 1,
        "查询密码": "需要" if extend.get("query_password_status") == 1 else "未知",
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
        "stock_label": str(stock_value) if stock_value not in (None, "") else "接口未公开数量",
        "sale_status": sale_status,
        "description": plain_text(item.get("description")),
        "image": str(item.get("image") or item.get("cover") or ""),
        "specs": specs,
        "limit_count": int(limit_count) if str(limit_count or "").isdigit() else None,
        "contact_format": str(item.get("contact_format") or "any"),
        "query_password_required": extend.get("query_password_status") == 1,
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
    result["query_password_required"] = result["specs"].get("查询密码") == "需要"
    legacy_limit = result["specs"].pop("单次限购", None)
    if "最低起购" not in result["specs"] and legacy_limit not in (None, ""):
        result["specs"]["最低起购"] = legacy_limit
    limit_value = result["specs"].get("最低起购")
    result["limit_count"] = int(limit_value) if str(limit_value).isdigit() else None
    return result


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
                    (product["source_url"], product["title"], DEFAULT_INTERVAL, stamp),
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
            watch["latest"] = serialize_snapshot(latest)
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
    with database() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = 'checkout'").fetchone()
        legacy = connection.execute("SELECT value FROM settings WHERE key = 'contact'").fetchone()
    fallback = _json_value(legacy["value"] if legacy else None, {"contact": "", "note": ""})
    fallback.update({"query_password": "", "channel_id": 1})
    return _json_value(row["value"] if row else None, fallback)


def _setting_json(key: str, fallback: dict[str, Any]) -> dict[str, Any]:
    with database() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    value = _json_value(row["value"] if row else None, fallback)
    return value if isinstance(value, dict) else dict(fallback)


def _normalize_service_url(value: Any, default: str) -> str:
    candidate = str(value or default).strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("服务地址必须是 http 或 https URL")
    if len(candidate) > 300:
        raise ValueError("服务地址过长")
    return candidate


def redeem_settings() -> dict[str, Any]:
    value = _setting_json("redeem", {"base_url": DEFAULT_REDEEM_URL})
    return {"base_url": _normalize_service_url(value.get("base_url"), DEFAULT_REDEEM_URL)}


def sub2api_settings(*, reveal: bool = False) -> dict[str, Any]:
    value = _setting_json("sub2api", {"base_url": DEFAULT_SUB2API_URL, "admin_key": ""})
    base_url = _normalize_service_url(value.get("base_url"), DEFAULT_SUB2API_URL)
    admin_key = str(value.get("admin_key") or "").strip()
    if reveal:
        return {"base_url": base_url, "admin_key": admin_key}
    masked = (admin_key[:4] + "..." + admin_key[-4:]) if len(admin_key) > 10 else ("*" * len(admin_key))
    return {"base_url": base_url, "admin_key_set": bool(admin_key), "admin_key_mask": masked}


def sub2api_automation_settings() -> dict[str, Any]:
    value = _setting_json("sub2api_automation", DEFAULT_SUB2API_AUTOMATION)
    try:
        interval = min(max(int(value.get("interval_seconds") or 300), 10), 86400)
    except (TypeError, ValueError):
        interval = 300
    proxy_id = value.get("proxy_id")
    try:
        proxy_id = int(proxy_id) if proxy_id not in (None, "") else None
    except (TypeError, ValueError):
        proxy_id = None
    raw_groups = value.get("group_ids")
    if not isinstance(raw_groups, list):
        raw_groups = []
    try:
        group_ids = sorted({int(item) for item in raw_groups if int(item) > 0})
    except (TypeError, ValueError):
        group_ids = []
    mode = str(value.get("codex_fingerprint_mode") or "off").strip().lower()
    if mode not in SUB2API_CODEX_FINGERPRINT_MODES:
        mode = "off"
    return {
        "enabled": bool(value.get("enabled", False)),
        "interval_seconds": interval,
        "auto_import": bool(value.get("auto_import", False)),
        "proxy_id": proxy_id,
        "group_ids": group_ids,
        "codex_fingerprint_mode": mode,
    }


def sub2api_automation_state() -> dict[str, Any]:
    fallback = {
        "last_run": None,
        "last_error": "",
        "last_result": None,
        "pending_card_codes": [],
        "imported_order_nos": [],
    }
    value = _setting_json("sub2api_automation_state", fallback)
    if not isinstance(value, dict):
        return fallback
    pending = value.get("pending_card_codes")
    value["pending_card_codes"] = pending if isinstance(pending, list) else []
    imported = value.get("imported_order_nos")
    value["imported_order_nos"] = imported if isinstance(imported, list) else []
    return {
        "last_run": value.get("last_run"),
        "last_error": str(value.get("last_error") or "")[:500],
        "last_result": value.get("last_result") if isinstance(value.get("last_result"), dict) else None,
        "pending_card_codes": value["pending_card_codes"][:100],
        "imported_order_nos": value["imported_order_nos"][-500:],
    }


def _store_sub2api_automation_state(value: dict[str, Any]) -> None:
    _store_setting("sub2api_automation_state", value)


def _store_setting(key: str, value: dict[str, Any]) -> None:
    with database() as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


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


def _validate_sub2api_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("账号 JSON 必须是对象")
    if data.get("type") not in (None, "sub2api-data", "sub2api-bundle"):
        raise ValueError("账号 JSON type 必须是 sub2api-data 或 sub2api-bundle")
    if data.get("version") not in (None, 1):
        raise ValueError("账号 JSON version 必须为 1")
    accounts = data.get("accounts")
    proxies = data.get("proxies", [])
    if not isinstance(accounts, list) or not accounts or len(accounts) > 5000:
        raise ValueError("账号 JSON 至少包含 1 个 accounts，最多 5000 个")
    if not isinstance(proxies, list) or len(proxies) > 5000:
        raise ValueError("账号 JSON proxies 格式无效")
    normalized_accounts = []
    for index, account in enumerate(accounts):
        if not isinstance(account, dict) or not isinstance(account.get("credentials"), dict) or not account.get("credentials"):
            raise ValueError(f"第 {index + 1} 个账号缺少 credentials")
        normalized_account = dict(account)
        normalized_account["name"] = str(account.get("name") or f"导入账号 {index + 1}")[:200]
        normalized_accounts.append(normalized_account)
    normalized = dict(data)
    normalized["type"] = data.get("type") or "sub2api-data"
    normalized["version"] = 1
    normalized["proxies"] = [dict(proxy) if isinstance(proxy, dict) else proxy for proxy in proxies]
    normalized["accounts"] = normalized_accounts
    return normalized


def _merge_sub2api_data(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return _validate_sub2api_data(data)
    if not isinstance(data, list) or not data or len(data) > 50:
        raise ValueError("账号 JSON 必须是对象，或 1 到 50 个对象组成的数组")
    accounts: list[dict[str, Any]] = []
    proxies: list[dict[str, Any]] = []
    for index, source in enumerate(data):
        try:
            normalized = _validate_sub2api_data(source)
        except ValueError as exc:
            raise ValueError(f"第 {index + 1} 个 JSON：{exc}") from exc
        accounts.extend(normalized["accounts"])
        proxies.extend(normalized["proxies"])
        if len(accounts) > 5000 or len(proxies) > 5000:
            raise ValueError("合并后的账号或代理数量不能超过 5000 个")
    return {
        "type": "sub2api-data",
        "version": 1,
        "accounts": accounts,
        "proxies": proxies,
    }


SUB2API_CODEX_FINGERPRINT_MODES = {"off", "device", "session", "full"}


def _sub2api_apply_codex_fingerprint_mode(
    normalized: dict[str, Any], raw_mode: Any
) -> tuple[dict[str, Any], str | None, int]:
    if raw_mode is None:
        return normalized, None, 0
    if not isinstance(raw_mode, str):
        raise ValueError("Codex 指纹收敛模式无效")
    mode = raw_mode.strip().lower()
    if mode not in SUB2API_CODEX_FINGERPRINT_MODES:
        raise ValueError("Codex 指纹收敛模式必须是 off、device、session 或 full")

    result = dict(normalized)
    accounts = []
    codex_account_count = 0
    for source in normalized["accounts"]:
        account = dict(source)
        if (
            str(account.get("platform") or "").strip().lower() == "openai"
            and str(account.get("type") or "").strip().lower() in ("oauth", "setup-token")
        ):
            extra = dict(account.get("extra")) if isinstance(account.get("extra"), dict) else {}
            if mode == "off":
                extra.pop("codex_fingerprint_mode", None)
            else:
                extra["codex_fingerprint_mode"] = mode
            account["extra"] = extra
            codex_account_count += 1
        accounts.append(account)
    result["accounts"] = accounts
    return result, mode, codex_account_count


def _sub2api_payload_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _sub2api_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    value = _sub2api_payload_data(payload)
    if isinstance(value, dict):
        for key in ("items", "list", *keys):
            candidate = value.get(key)
            if isinstance(candidate, list):
                value = candidate
                break
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _sub2api_upstream_error(status: int, payload: Any, raw: str) -> str:
    if isinstance(payload, dict):
        detail = payload.get("message") or payload.get("error") or payload.get("detail")
        if detail:
            return str(detail)[:500]
    return raw[:500] or f"HTTP {status}"


def _sub2api_fetch_accounts(config: dict[str, Any], *, platform: str = "", account_type: str = "") -> list[dict[str, Any]]:
    headers = {"x-api-key": config["admin_key"]}
    accounts: list[dict[str, Any]] = []
    page = 1
    while page <= 200:
        params = {
            "page": page,
            "page_size": 100,
            "sort_by": "created_at",
            "sort_order": "desc",
        }
        if platform:
            params["platform"] = platform
        if account_type:
            params["type"] = account_type
        status, payload, raw = _external_json_request(
            "GET",
            config["base_url"] + "/api/v1/admin/accounts?" + urlencode(params),
            headers=headers,
            timeout=30,
        )
        if not 200 <= status < 300:
            detail = _sub2api_upstream_error(status, payload, raw)
            raise RuntimeError(f"Sub2API accounts 返回 HTTP {status}: {detail}")
        value = _sub2api_payload_data(payload)
        items = _sub2api_list(payload, "accounts")
        accounts.extend(items)
        if not isinstance(value, dict):
            break
        try:
            pages_value = value.get("pages")
            if pages_value is not None:
                pages = max(1, int(pages_value))
            else:
                total = max(0, int(value.get("total") or 0))
                size = max(1, int(value.get("page_size") or params["page_size"]))
                pages = max(1, (total + size - 1) // size)
        except (TypeError, ValueError):
            pages = 1
        if page >= pages or not items:
            break
        page += 1
    if page > 200:
        raise RuntimeError("Sub2API 账号分页超过 200 页，请先缩小账号范围")
    return accounts


def _sub2api_codex_accounts(config: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for account_type in ("oauth", "setup-token"):
        result.extend(_sub2api_fetch_accounts(config, platform="openai", account_type=account_type))
    return result


def _sub2api_created_account_ids(payload: Any) -> set[int]:
    result = _sub2api_payload_data(payload)
    rows = result.get("results") if isinstance(result, dict) else None
    ids: set[int] = set()
    if not isinstance(rows, list):
        return ids
    for row in rows:
        if not isinstance(row, dict) or row.get("success") is False:
            continue
        try:
            account_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if account_id > 0:
            ids.add(account_id)
    return ids


def _sub2api_fingerprint_targets(
    imported_accounts: list[dict[str, Any]],
    upstream_accounts: list[dict[str, Any]],
    created_ids: set[int],
) -> list[dict[str, Any]]:
    expected: dict[tuple[str, str, str], int] = {}
    for account in imported_accounts:
        platform = str(account.get("platform") or "").strip().lower()
        account_type = str(account.get("type") or "").strip().lower()
        if platform != "openai" or account_type not in ("oauth", "setup-token"):
            continue
        key = (str(account.get("name") or ""), platform, account_type)
        expected[key] = expected.get(key, 0) + 1

    candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for account in upstream_accounts:
        try:
            account_id = int(account.get("id"))
        except (TypeError, ValueError):
            continue
        if created_ids and account_id not in created_ids:
            continue
        key = (
            str(account.get("name") or ""),
            str(account.get("platform") or "").strip().lower(),
            str(account.get("type") or "").strip().lower(),
        )
        if key in expected:
            candidates.setdefault(key, []).append(account)

    targets = []
    for key, count in expected.items():
        rows = sorted(candidates.get(key, []), key=lambda item: int(item.get("id") or 0), reverse=True)
        targets.extend(rows[:count])
    return targets


def _sub2api_reconcile_codex_fingerprint(
    config: dict[str, Any],
    normalized: dict[str, Any],
    mode: str | None,
    import_payload: Any,
) -> dict[str, Any]:
    eligible = [
        account for account in normalized["accounts"]
        if str(account.get("platform") or "").strip().lower() == "openai"
        and str(account.get("type") or "").strip().lower() in ("oauth", "setup-token")
    ]
    summary = {
        "mode": mode,
        "eligible": len(eligible),
        "matched": 0,
        "verified": 0,
        "repaired": 0,
        "unresolved": len(eligible),
    }
    if mode is None or not eligible:
        return summary

    upstream_accounts = _sub2api_codex_accounts(config)
    created_ids = _sub2api_created_account_ids(import_payload)
    targets = _sub2api_fingerprint_targets(eligible, upstream_accounts, created_ids)
    summary["matched"] = len(targets)
    expected_values = {None, "", "off"} if mode == "off" else {mode}
    mismatched_ids = []
    for account in targets:
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        actual = str(extra.get("codex_fingerprint_mode") or "").strip().lower() or None
        if actual in expected_values:
            summary["verified"] += 1
        else:
            try:
                mismatched_ids.append(int(account["id"]))
            except (KeyError, TypeError, ValueError):
                continue

    if mismatched_ids:
        status, payload, raw = _external_json_request(
            "POST",
            config["base_url"] + "/api/v1/admin/accounts/bulk-update",
            {"account_ids": mismatched_ids, "extra": {"codex_fingerprint_mode": mode}},
            headers={"x-api-key": config["admin_key"]},
            timeout=60,
        )
        if not 200 <= status < 300:
            detail = _sub2api_upstream_error(status, payload, raw)
            raise RuntimeError(f"Sub2API 指纹补写返回 HTTP {status}: {detail}")
        result = _sub2api_payload_data(payload)
        success_ids = result.get("success_ids") if isinstance(result, dict) else None
        submitted_ids = {
            int(account_id) for account_id in (success_ids if isinstance(success_ids, list) else mismatched_ids)
            if str(account_id).isdigit() and int(account_id) > 0
        }
        refreshed = _sub2api_codex_accounts(config)
        for account in refreshed:
            try:
                account_id = int(account.get("id"))
            except (TypeError, ValueError):
                continue
            if account_id not in submitted_ids:
                continue
            extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
            actual = str(extra.get("codex_fingerprint_mode") or "").strip().lower() or None
            if actual in expected_values:
                summary["repaired"] += 1
        summary["verified"] += summary["repaired"]

    summary["unresolved"] = max(0, summary["eligible"] - summary["verified"])
    return summary


def _sub2api_fingerprint_verification_error(mode: str | None, eligible: int, error: Exception) -> dict[str, Any]:
    return {
        "mode": mode,
        "eligible": eligible,
        "matched": 0,
        "verified": 0,
        "repaired": 0,
        "unresolved": eligible,
        "error": str(error)[:500],
    }


SUB2API_401_TEXT_PATTERNS = (
    re.compile(r"(?i)\bhttp(?:/[0-9.]+)?\s*401\b"),
    re.compile(r"(?i)\b(?:oauth|status(?:_code|\s+code)?|code)\s*[:=()\[\]-]*\s*401\b"),
    re.compile(r"(?i)\bunauthorized\s*[:=()\[\]-]*\s*401\b"),
    re.compile(r"(?i)\b401\s*[:=()\[\]-]*\s*unauthorized\b"),
    # Sub2API reports revoked OAuth credentials as `Token revoked (401)`.
    re.compile(r"(?i)\b(?:token\s+revoked|invalid(?:ated)?\s+oauth\s+token)\b[^\r\n]{0,80}\(\s*401\s*\)"),
)


def _sub2api_error_text_is_401(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in SUB2API_401_TEXT_PATTERNS)


def _sub2api_structured_error_is_401(value: Any, *, error_context: bool = False) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).strip().lower()
            child_context = error_context or any(token in normalized_key for token in ("error", "reason", "failure", "response"))
            if normalized_key in ("status_code", "http_status", "http_status_code", "upstream_status"):
                try:
                    if int(child) == 401:
                        return True
                except (TypeError, ValueError):
                    pass
            if child_context and normalized_key in ("status", "code"):
                try:
                    if int(child) == 401:
                        return True
                except (TypeError, ValueError):
                    pass
            if child_context and _sub2api_error_text_is_401(child):
                return True
            if child_context and _sub2api_structured_error_is_401(child, error_context=True):
                return True
        return False
    if isinstance(value, list):
        return error_context and any(_sub2api_structured_error_is_401(item, error_context=True) for item in value)
    return error_context and _sub2api_error_text_is_401(value)


def _sub2api_account_is_401(account: dict[str, Any]) -> bool:
    for key in ("status_code", "http_status", "http_status_code", "upstream_status", "status"):
        try:
            if int(account.get(key)) == 401:
                return True
        except (TypeError, ValueError):
            pass
    for key in ("error_message", "temp_unschedulable_reason", "last_error", "error", "detail"):
        value = account.get(key)
        if _sub2api_error_text_is_401(value) or _sub2api_structured_error_is_401(value, error_context=True):
            return True
    extra = account.get("extra")
    return _sub2api_structured_error_is_401(extra, error_context=False)


def _download_reclaim_payloads(
    reclaim_result: dict[str, Any], client: Any, *, exclude_order_nos: list[str] | None = None
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    total_bytes = 0
    excluded = {str(value) for value in (exclude_order_nos or []) if str(value)}
    tasks = reclaim_result.get("all_tasks") if isinstance(reclaim_result, dict) else None
    if not isinstance(tasks, list):
        return payloads
    for task in tasks[:100]:
        if not isinstance(task, dict) or task.get("status") != "done":
            continue
        order_no = str(task.get("order_no") or "").strip()
        token = str(task.get("download_token") or "").strip()
        if not order_no or not token or order_no in excluded:
            continue
        try:
            content = client.download(order_no, token)
        except Exception:
            continue
        if not content or len(content) > MAX_EXTERNAL_JSON_BYTES or total_bytes + len(content) > 24 * 1024 * 1024:
            continue
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict) or not isinstance(parsed.get("accounts"), list) or not parsed["accounts"]:
            continue
        total_bytes += len(content)
        payloads.append({
            "task": task,
            "filename": f"{order_no}.json",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "data": parsed,
        })
    return payloads


def reclaim_sub2api_401_accounts(
    *, include_downloads: bool = True, exclude_order_nos: list[str] | None = None
) -> dict[str, Any]:
    config = sub2api_settings(reveal=True)
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    accounts = _sub2api_fetch_accounts(config)
    accounts_401 = [account for account in accounts if _sub2api_account_is_401(account)]
    from redeem_api_sdk import extract_card_code_from_name

    card_codes = []
    missing = []
    seen = set()
    for account in accounts_401:
        card_code = extract_card_code_from_name(str(account.get("name") or ""))
        if not card_code or len(card_code) < 8 or "-" not in card_code:
            missing.append({"id": account.get("id"), "name": str(account.get("name") or "")[:200]})
            continue
        if card_code not in seen:
            seen.add(card_code)
            card_codes.append(card_code)

    reclaim_result = None
    downloaded_payloads: list[dict[str, Any]] = []
    if card_codes:
        client = _redeem_client()
        reclaim_result = _dataclass_to_json(client.batch_reclaim(card_codes, mode="401"))
        if include_downloads and isinstance(reclaim_result, dict):
            downloaded_payloads = _download_reclaim_payloads(
                reclaim_result, client, exclude_order_nos=exclude_order_nos
            )
    return {
        "ok": reclaim_result is None or bool(reclaim_result.get("ok", False)),
        "scanned_accounts": len(accounts),
        "accounts_401": len(accounts_401),
        "card_code_count": len(card_codes),
        "missing_card_code_count": len(missing),
        "skipped_non_401": len(accounts) - len(accounts_401),
        "submitted": bool(card_codes),
        "missing_card_code_accounts": missing[:50],
        "reclaim_card_codes": card_codes,
        "downloaded_payloads": downloaded_payloads,
        "result": reclaim_result,
    }


def fetch_sub2api_options() -> dict[str, Any]:
    config = sub2api_settings(reveal=True)
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    headers = {"x-api-key": config["admin_key"]}
    endpoints = {
        "proxies": "/api/v1/admin/proxies/all?with_count=true",
        "groups": "/api/v1/admin/groups/all",
    }
    responses: dict[str, Any] = {}
    for key, endpoint in endpoints.items():
        status, payload, raw = _external_json_request(
            "GET", config["base_url"] + endpoint, headers=headers, timeout=20
        )
        if not 200 <= status < 300:
            detail = _sub2api_upstream_error(status, payload, raw)
            raise RuntimeError(f"Sub2API {key} 返回 HTTP {status}: {detail}")
        responses[key] = payload

    proxies = []
    for item in _sub2api_list(responses["proxies"], "proxies"):
        try:
            proxy_id = int(item.get("id"))
            port = int(item.get("port") or 0)
            account_count = int(item.get("account_count") or 0)
        except (TypeError, ValueError):
            continue
        if proxy_id < 1:
            continue
        proxies.append({
            "id": proxy_id,
            "name": str(item.get("name") or f"代理 {proxy_id}")[:160],
            "protocol": str(item.get("protocol") or ""),
            "host": str(item.get("host") or "")[:255],
            "port": port,
            "status": str(item.get("status") or ""),
            "account_count": account_count,
        })

    groups = []
    for item in _sub2api_list(responses["groups"], "groups"):
        try:
            group_id = int(item.get("id"))
            account_count = int(item.get("account_count") or 0)
        except (TypeError, ValueError):
            continue
        if group_id < 1:
            continue
        groups.append({
            "id": group_id,
            "name": str(item.get("name") or f"分组 {group_id}")[:160],
            "platform": str(item.get("platform") or ""),
            "status": str(item.get("status") or ""),
            "account_count": account_count,
        })

    return {
        "ok": True,
        "proxy_service_available": True,
        "proxy_count": len(proxies),
        "group_count": len(groups),
        "proxies": proxies,
        "groups": groups,
    }


def _sub2api_assignment_payload(
    normalized: dict[str, Any], proxy_id: Any, raw_group_ids: Any
) -> tuple[dict[str, Any], int | None, list[int]]:
    selected_proxy_id: int | None = None
    if proxy_id not in (None, ""):
        try:
            selected_proxy_id = int(proxy_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Sub2API 代理编号无效") from exc
        if selected_proxy_id < 1:
            raise ValueError("Sub2API 代理编号无效")

    if raw_group_ids is None:
        raw_group_ids = []
    if not isinstance(raw_group_ids, list) or len(raw_group_ids) > 100:
        raise ValueError("Sub2API 分组必须是最多 100 项的数组")
    try:
        group_ids = sorted({int(value) for value in raw_group_ids})
    except (TypeError, ValueError) as exc:
        raise ValueError("Sub2API 分组编号无效") from exc
    if any(value < 1 for value in group_ids):
        raise ValueError("Sub2API 分组编号无效")

    accounts = []
    for index, source in enumerate(normalized["accounts"]):
        platform = str(source.get("platform") or "").strip()
        account_type = str(source.get("type") or "").strip()
        if not platform or not account_type:
            raise ValueError(f"第 {index + 1} 个账号缺少 platform 或 type")
        try:
            concurrency = int(source.get("concurrency") or 0)
            priority = int(source.get("priority") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {index + 1} 个账号 concurrency 或 priority 无效") from exc
        item: dict[str, Any] = {
            "name": str(source.get("name") or f"导入账号 {index + 1}")[:200],
            "platform": platform,
            "type": account_type,
            "credentials": source["credentials"],
            "extra": source.get("extra") if isinstance(source.get("extra"), dict) else {},
            "proxy_id": selected_proxy_id,
            "concurrency": concurrency,
            "priority": priority,
            "group_ids": group_ids,
        }
        for key in ("notes", "rate_multiplier", "expires_at", "auto_pause_on_expired"):
            if key in source:
                item[key] = source[key]
        accounts.append(item)
    return {"accounts": accounts}, selected_proxy_id, group_ids


def _sub2api_import_payload(
    source: Any,
    *,
    proxy_id: Any = None,
    group_ids: Any = None,
    codex_fingerprint_mode: Any = None,
    assign_existing: bool | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    config = sub2api_settings(reveal=True)
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    normalized = _merge_sub2api_data(source)
    normalized, fingerprint_mode, codex_account_count = _sub2api_apply_codex_fingerprint_mode(
        normalized, codex_fingerprint_mode
    )
    if assign_existing is None:
        assign_existing = proxy_id not in (None, "") or bool(group_ids)
    if assign_existing:
        request_payload, selected_proxy_id, selected_group_ids = _sub2api_assignment_payload(
            normalized, proxy_id, group_ids
        )
        target_endpoint = config["base_url"] + "/api/v1/admin/accounts/batch"
        mode = "assigned"
    else:
        selected_proxy_id, selected_group_ids = None, []
        import_endpoint = endpoint or "/api/v1/admin/accounts/data"
        if import_endpoint == "/api/v1/admin/accounts/data":
            request_payload = {"data": normalized, "skip_default_group_bind": True}
        elif import_endpoint == "/api/v1/admin/accounts/import/codex-session":
            request_payload = {"content": json.dumps(normalized, ensure_ascii=False), "update_existing": True}
        else:
            raise ValueError("Sub2API 导入接口无效")
        target_endpoint = config["base_url"] + import_endpoint
        mode = "data"
    status, payload, raw = _external_json_request(
        "POST",
        target_endpoint,
        request_payload,
        headers={"x-api-key": config["admin_key"]},
        timeout=60,
    )
    if not 200 <= status < 300:
        detail = _sub2api_upstream_error(status, payload, raw)
        raise RuntimeError(f"Sub2API 返回 HTTP {status}: {detail}")
    try:
        fingerprint_verification = _sub2api_reconcile_codex_fingerprint(
            config, normalized, fingerprint_mode, payload
        )
    except RuntimeError as exc:
        fingerprint_verification = _sub2api_fingerprint_verification_error(
            fingerprint_mode, codex_account_count, exc
        )
    return {
        "ok": True,
        "mode": mode,
        "upstream_status": status,
        "proxy_id": selected_proxy_id,
        "group_ids": selected_group_ids,
        "codex_fingerprint_mode": fingerprint_mode,
        "codex_account_count": codex_account_count,
        "fingerprint_verification": fingerprint_verification,
        "result": _sub2api_payload_data(payload),
    }


def save_sub2api_automation_settings(data: dict[str, Any]) -> dict[str, Any]:
    try:
        interval = min(max(int(data.get("interval_seconds") or 300), 10), 86400)
    except (TypeError, ValueError) as exc:
        raise ValueError("自动监控间隔必须是 10 到 86400 秒") from exc
    mode = str(data.get("codex_fingerprint_mode") or "off").strip().lower()
    if mode not in SUB2API_CODEX_FINGERPRINT_MODES:
        raise ValueError("Codex 指纹收敛模式无效")
    proxy_id = data.get("proxy_id")
    try:
        proxy_id = int(proxy_id) if proxy_id not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("自动导入代理无效") from exc
    raw_groups = data.get("group_ids") or []
    if not isinstance(raw_groups, list) or len(raw_groups) > 100:
        raise ValueError("自动导入分组无效")
    try:
        group_ids = sorted({int(item) for item in raw_groups})
    except (TypeError, ValueError) as exc:
        raise ValueError("自动导入分组无效") from exc
    if any(item < 1 for item in group_ids):
        raise ValueError("自动导入分组无效")
    enabled = bool(data.get("enabled", False))
    auto_import = bool(data.get("auto_import", False))
    if enabled:
        if not sub2api_settings(reveal=True)["admin_key"]:
            raise ValueError("请先配置 Sub2API 管理员密钥")
        if not auto_import:
            raise ValueError("启用自动监控前必须勾选自动导入")
        if proxy_id is None or not group_ids or mode == "off":
            raise ValueError("启用自动监控前必须选择代理、至少一个分组和 Codex 指纹模式")
    value = {
        "enabled": enabled,
        "interval_seconds": interval,
        "auto_import": auto_import,
        "proxy_id": proxy_id,
        "group_ids": group_ids,
        "codex_fingerprint_mode": mode,
    }
    _store_setting("sub2api_automation", value)
    return value


def refresh_sub2api_reclaim(
    card_codes: list[str], *, exclude_order_nos: list[str] | None = None
) -> dict[str, Any]:
    normalized = list(dict.fromkeys(str(code).strip() for code in card_codes if str(code).strip()))
    if not normalized or len(normalized) > 100:
        raise ValueError("卡密数量应为 1 到 100 个")
    client = _redeem_client()
    result = _dataclass_to_json(client.refresh_progress(normalized))
    downloads = (
        _download_reclaim_payloads(result, client, exclude_order_nos=exclude_order_nos)
        if isinstance(result, dict)
        else []
    )
    return {
        "ok": bool(isinstance(result, dict) and result.get("ok", False)),
        "reclaim_card_codes": normalized,
        "downloaded_payloads": downloads,
        "result": result,
    }


def run_sub2api_automation_cycle() -> dict[str, Any]:
    settings = sub2api_automation_settings()
    state = sub2api_automation_state()
    if not settings["enabled"]:
        return {"ok": True, "skipped": True, "reason": "disabled", "settings": settings, "state": state}
    pending = state["pending_card_codes"]
    imported_order_nos = state["imported_order_nos"] if pending else []
    if pending:
        reclaim = refresh_sub2api_reclaim(pending, exclude_order_nos=imported_order_nos)
    else:
        reclaim = reclaim_sub2api_401_accounts(
            include_downloads=True, exclude_order_nos=imported_order_nos
        )
    if not reclaim.get("ok", False):
        failure = reclaim.get("result") if isinstance(reclaim.get("result"), dict) else {}
        raise RuntimeError(str(failure.get("error") or reclaim.get("error") or "401 找回服务返回失败")[:500])
    result = reclaim.get("result") if isinstance(reclaim.get("result"), dict) else {}
    downloads = reclaim.get("downloaded_payloads") if isinstance(reclaim.get("downloaded_payloads"), list) else []
    card_codes = reclaim.get("reclaim_card_codes") if isinstance(reclaim.get("reclaim_card_codes"), list) else pending
    import_result = None
    if downloads:
        import_result = _sub2api_import_payload(
            [item["data"] for item in downloads if isinstance(item, dict) and isinstance(item.get("data"), dict)],
            proxy_id=settings["proxy_id"],
            group_ids=settings["group_ids"],
            codex_fingerprint_mode=settings["codex_fingerprint_mode"],
            assign_existing=True,
        )
        imported_order_nos = (
            imported_order_nos
            + [str(item.get("task", {}).get("order_no") or "") for item in downloads]
        )[-500:]
    if int(result.get("queued") or 0) + int(result.get("already_running") or 0) > 0:
        pending = card_codes
    else:
        pending = []
        imported_order_nos = []
    summary = {
        "ok": bool(reclaim.get("ok", False)),
        "scanned_accounts": reclaim.get("scanned_accounts"),
        "accounts_401": reclaim.get("accounts_401"),
        "card_code_count": reclaim.get("card_code_count", len(card_codes or [])),
        "queued": int(result.get("queued") or 0),
        "done": int(result.get("done") or 0),
        "downloaded": len(downloads),
        "imported": bool(import_result),
        "import_result": import_result,
    }
    state = {
        "last_run": utc_now(),
        "last_error": "",
        "last_result": summary,
        "pending_card_codes": pending,
        "imported_order_nos": imported_order_nos,
    }
    _store_sub2api_automation_state(state)
    return {"ok": True, "settings": settings, "state": state, "result": summary}


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
            product = serialize_snapshot(latest)
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
            product = serialize_snapshot(latest)
            title = product.get("title") or watch["name"] or f"商品 {watch_id}"
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
    """Return a paged history view plus compact statistics for the price radar."""
    limit = min(max(int(limit or 25), 1), 200)
    offset = max(int(offset or 0), 0)
    status = status if status in {"all", "success", "error"} else "all"
    stock = stock if stock in {"all", "in", "out", "unknown"} else "all"
    start_date = str(start_date or "").strip()[:10]
    end_date = str(end_date or "").strip()[:10]
    query = str(query or "").strip()[:120]

    clauses = ["watch_id = ?"]
    params: list[Any] = [watch_id]
    if start_date:
        clauses.append("fetched_at >= ?")
        params.append(f"{start_date}T00:00:00")
    if end_date:
        clauses.append("fetched_at <= ?")
        params.append(f"{end_date}T23:59:59")
    if status != "all":
        clauses.append("status = ?")
        params.append(status)
    numeric_stock = "stock IS NOT NULL AND stock != '' AND stock NOT GLOB '*[^0-9]*'"
    if stock == "in":
        clauses.append(f"{numeric_stock} AND CAST(stock AS INTEGER) > 0")
    elif stock == "out":
        clauses.append(f"{numeric_stock} AND CAST(stock AS INTEGER) <= 0")
    elif stock == "unknown":
        clauses.append(f"NOT ({numeric_stock})")
    if query:
        clauses.append("(title LIKE ? OR price LIKE ? OR stock LIKE ? OR sale_status LIKE ? OR error LIKE ?)")
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern, pattern, pattern])

    where_sql = " AND ".join(clauses)
    with database() as connection:
        exists = connection.execute("SELECT 1 FROM watches WHERE id = ?", (watch_id,)).fetchone()
        if not exists:
            raise KeyError("监控商品不存在")
        total = int(connection.execute(f"SELECT COUNT(*) FROM snapshots WHERE {where_sql}", params).fetchone()[0])
        stats_row = connection.execute(
            f"""
            SELECT
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
                COUNT(CASE WHEN status = 'success' AND price IS NOT NULL AND price != '' THEN 1 END) AS quoted_count,
                MIN(CASE WHEN status = 'success' AND price IS NOT NULL AND price != '' THEN CAST(price AS REAL) END) AS min_price,
                MAX(CASE WHEN status = 'success' AND price IS NOT NULL AND price != '' THEN CAST(price AS REAL) END) AS max_price,
                AVG(CASE WHEN status = 'success' AND price IS NOT NULL AND price != '' THEN CAST(price AS REAL) END) AS average_price,
                SUM(CASE WHEN {numeric_stock} AND CAST(stock AS INTEGER) > 0 THEN 1 ELSE 0 END) AS in_stock_count,
                SUM(CASE WHEN {numeric_stock} AND CAST(stock AS INTEGER) <= 0 THEN 1 ELSE 0 END) AS out_stock_count,
                SUM(CASE WHEN NOT ({numeric_stock}) THEN 1 ELSE 0 END) AS unknown_stock_count
            FROM snapshots WHERE {where_sql}
            """,
            params,
        ).fetchone()
        page_params = [*params, limit, offset]
        rows = connection.execute(
            f"""
            SELECT id, title, price, stock, sale_status, fetched_at, status, error
            FROM snapshots WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            page_params,
        ).fetchall()
        trend_rows = connection.execute(
            f"""
            SELECT id, price, stock, sale_status, fetched_at, status, error FROM (
                SELECT id, price, stock, sale_status, fetched_at, status, error
                FROM snapshots WHERE {where_sql} AND status = 'success'
                ORDER BY id DESC LIMIT 240
            ) ORDER BY id ASC
            """,
            params,
        ).fetchall()

    def serialize_history_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        raw_stock = result.get("stock")
        result["stock"] = int(raw_stock) if str(raw_stock or "").isdigit() else None
        result["stock_label"] = str(raw_stock) if raw_stock not in (None, "") else "接口未公开数量"
        return result

    stats = {
        "success_count": int(stats_row["success_count"] or 0),
        "error_count": int(stats_row["error_count"] or 0),
        "quoted_count": int(stats_row["quoted_count"] or 0),
        "min_price": _money(stats_row["min_price"]) if stats_row["min_price"] is not None else None,
        "max_price": _money(stats_row["max_price"]) if stats_row["max_price"] is not None else None,
        "average_price": _money(stats_row["average_price"]) if stats_row["average_price"] is not None else None,
        "in_stock_count": int(stats_row["in_stock_count"] or 0),
        "out_stock_count": int(stats_row["out_stock_count"] or 0),
        "unknown_stock_count": int(stats_row["unknown_stock_count"] or 0),
    }
    return {
        "items": [serialize_history_row(row) for row in rows],
        "trend": [serialize_history_row(row) for row in trend_rows],
        "total": total,
        "page": offset // limit + 1,
        "page_size": limit,
        "stats": stats,
        "filters": {
            "start_date": start_date,
            "end_date": end_date,
            "status": status,
            "stock": stock,
            "query": query,
        },
    }


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


class MonitorWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="product-monitor", daemon=True)
        self.stop_event = threading.Event()
        self.fetch_lock = threading.Lock()

    def run(self) -> None:
        while not self.stop_event.wait(0.25):
            current = time.time()
            shop_refresh_cache: dict[int, dict[str, Any] | Exception] = {}
            with database() as connection:
                preorder_rows = connection.execute(
                    """
                    SELECT id, watch_id, last_check, interval_seconds FROM preorders
                    WHERE enabled = 1 AND status = 'watching'
                    """
                ).fetchall()
            for row in preorder_rows:
                last_check = 0.0
                if row["last_check"]:
                    try:
                        last_check = datetime.fromisoformat(row["last_check"]).timestamp()
                    except ValueError:
                        pass
                if current - last_check < int(row["interval_seconds"] or 1):
                    continue
                if not self.fetch_lock.acquire(blocking=False):
                    break
                try:
                    product = record_inventory_fetch(row["watch_id"], shop_refresh_cache)
                    process_preorder(row["id"], product)
                except Exception as exc:
                    mark_preorder_check_error(row["id"], exc)
                finally:
                    self.fetch_lock.release()

            with database() as connection:
                rows = connection.execute(
                    "SELECT id, last_run, interval_seconds FROM watches WHERE enabled = 1"
                ).fetchall()
            for row in rows:
                last_run = 0.0
                if row["last_run"]:
                    try:
                        last_run = datetime.fromisoformat(row["last_run"]).timestamp()
                    except ValueError:
                        pass
                if current - last_run < int(row["interval_seconds"] or DEFAULT_INTERVAL):
                    continue
                if not self.fetch_lock.acquire(blocking=False):
                    break
                try:
                    record_inventory_fetch(row["id"], shop_refresh_cache)
                except Exception:
                    pass
                finally:
                    self.fetch_lock.release()

            with database() as connection:
                shop_rows = connection.execute(
                    """
                    SELECT s.id, s.last_run, s.interval_seconds,
                        (SELECT error FROM shop_runs WHERE shop_id = s.id ORDER BY id DESC LIMIT 1) AS last_error
                    FROM shops s WHERE s.enabled = 1
                    """
                ).fetchall()
            for row in shop_rows:
                last_run = 0.0
                if row["last_run"]:
                    try:
                        last_run = datetime.fromisoformat(row["last_run"]).timestamp()
                    except ValueError:
                        pass
                retry_interval = int(row["interval_seconds"] or 300)
                if "WAF" in str(row["last_error"] or ""):
                    retry_interval = max(retry_interval, 3600)
                if current - last_run < retry_interval:
                    continue
                if row["id"] in shop_refresh_cache:
                    continue
                if not self.fetch_lock.acquire(blocking=False):
                    break
                try:
                    shop_refresh_cache[row["id"]] = record_shop_fetch(row["id"])
                except Exception as exc:
                    shop_refresh_cache[row["id"]] = exc
                finally:
                    self.fetch_lock.release()

    def fetch(self, watch_id: int) -> dict[str, Any]:
        with self.fetch_lock:
            return record_inventory_fetch(watch_id)

    def fetch_many(self, watch_ids: list[int]) -> list[dict[str, Any]]:
        with self.fetch_lock:
            shop_refresh_cache: dict[int, dict[str, Any] | Exception] = {}
            results = []
            for watch_id in watch_ids:
                try:
                    results.append({
                        "id": watch_id,
                        "ok": True,
                        "data": record_inventory_fetch(watch_id, shop_refresh_cache),
                    })
                except Exception as exc:
                    results.append({"id": watch_id, "ok": False, "error": str(exc)[:240]})
            return results

    def fetch_shop(self, shop_id: int) -> dict[str, Any]:
        with self.fetch_lock:
            return record_shop_fetch(shop_id)


WORKER = MonitorWorker()


class Sub2ApiAutomationWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="sub2api-401-automation", daemon=True)
        self.stop_event = threading.Event()
        self.run_lock = threading.Lock()

    def run_once(self) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            return {"ok": True, "skipped": True, "reason": "busy"}
        try:
            return run_sub2api_automation_cycle()
        except Exception as exc:
            previous = sub2api_automation_state()
            state = {
                "last_run": utc_now(),
                "last_error": str(exc)[:500],
                "last_result": previous.get("last_result"),
                "pending_card_codes": previous.get("pending_card_codes", []),
                "imported_order_nos": previous.get("imported_order_nos", []),
            }
            _store_sub2api_automation_state(state)
            return {"ok": False, "detail": state["last_error"], "state": state}
        finally:
            self.run_lock.release()

    def run(self) -> None:
        while not self.stop_event.wait(1):
            settings = sub2api_automation_settings()
            if not settings["enabled"]:
                continue
            state = sub2api_automation_state()
            last_run = 0.0
            if state.get("last_run"):
                try:
                    last_run = datetime.fromisoformat(str(state["last_run"])).timestamp()
                except ValueError:
                    pass
            interval = min(settings["interval_seconds"], 10) if state["pending_card_codes"] else settings["interval_seconds"]
            if time.time() - last_run >= interval:
                self.run_once()


AUTOMATION_WORKER = Sub2ApiAutomationWorker()


class BrowserVerificationManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.driver: Any = None
        self.shop_id: int | None = None

    @staticmethod
    def _request_data(shop: sqlite3.Row, current: int) -> dict[str, Any]:
        return {
            "token": shop["token"],
            "keywords": shop["keywords"] or "",
            "category_id": shop["category_id"] or "",
            "goods_type": shop["goods_type"] or "card",
            "current": current,
            "pageSize": 50,
        }

    @staticmethod
    def _is_waf_html(text: str) -> bool:
        encoded = text.encode("utf-8", "ignore")
        return any(marker in encoded for marker in WAF_MARKERS)

    def _close(self) -> None:
        driver, self.driver, self.shop_id = self.driver, None, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def _browser_request(self, driver: Any, data: dict[str, Any]) -> dict[str, Any]:
        driver.set_script_timeout(30)
        result = driver.execute_async_script(
            """
            const payload = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch('/shopApi/Shop/goodsList', {
              method: 'POST',
              credentials: 'include',
              headers: {'Accept': 'application/json, text/plain, */*', 'Content-Type': 'application/json'},
              body: JSON.stringify(payload)
            }).then(async response => done({
              status: response.status,
              content_type: response.headers.get('content-type') || '',
              text: await response.text()
            })).catch(error => done({error: String(error)}));
            """,
            data,
        )
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError(str((result or {}).get("error") or "浏览器同步请求失败")[:200])
        text = str(result.get("text") or "")
        if self._is_waf_html(text):
            raise WafChallengeRequired(text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("浏览器会话仍未返回商品 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("浏览器会话返回的商品数据格式无效")
        return payload

    def _catalog(self, driver: Any, shop: sqlite3.Row, first_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        products: dict[str, dict[str, Any]] = {}
        for current in range(1, 51):
            payload = first_payload if current == 1 and first_payload is not None else self._browser_request(
                driver, self._request_data(shop, current)
            )
            rows, pagination = _goods_list_rows(payload)
            for row in rows:
                product = normalize_goods_list_item(row, shop["token"])
                products[product["goods_key"]] = product
            total_value = _first_value(pagination, ("total", "count", "total_count"))
            page_value = _first_value(pagination, ("last_page", "lastPage", "pages", "page_count"))
            try:
                total = int(total_value) if total_value not in (None, "") else None
            except (TypeError, ValueError):
                total = None
            try:
                pages = int(page_value) if page_value not in (None, "") else None
            except (TypeError, ValueError):
                pages = None
            if not rows or len(rows) < 50 or (total is not None and current * 50 >= total) or (pages is not None and current >= pages):
                break
        return list(products.values())

    def _render_challenge(self, driver: Any, html_text: str) -> dict[str, Any]:
        try:
            driver.execute_script("document.open(); document.write(arguments[0]); document.close();", html_text)
        except Exception as exc:
            self._close()
            raise RuntimeError("无法在 Edge 中显示滑块验证页") from exc
        return {
            "status": "awaiting_verification",
            "detail": "请在已打开的 Edge 窗口完成滑块，然后点击“验证完成并同步”",
        }

    def start(self, shop_id: int) -> dict[str, Any]:
        with self.lock:
            with database() as connection:
                shop = connection.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
            if shop is None:
                raise KeyError("监控店铺不存在")
            self._close()
            try:
                from selenium import webdriver
                from selenium.webdriver.edge.options import Options
            except ImportError as exc:
                raise RuntimeError("缺少浏览器验证组件，请执行 python -m pip install -r backend/requirements.txt") from exc
            options = Options()
            options.add_argument(f"--user-data-dir={Path(__file__).with_name('waf-browser-profile')}")
            options.add_argument("--start-maximized")
            options.add_argument("--no-first-run")
            options.add_argument("--disable-features=EdgeFirstRunExperience")
            try:
                driver = webdriver.Edge(options=options)
            except Exception as exc:
                raise RuntimeError("无法启动 Edge 浏览器验证会话") from exc
            self.driver, self.shop_id = driver, shop_id
            try:
                driver.get(shop["url"])
                first_payload = self._browser_request(driver, self._request_data(shop, 1))
                products = self._catalog(driver, shop, first_payload)
                with WORKER.fetch_lock:
                    summary = record_shop_fetch(shop_id, products_override=products)
                self._close()
                return {"status": "success", "summary": summary}
            except WafChallengeRequired as exc:
                return self._render_challenge(driver, str(exc))
            except Exception as exc:
                self._close()
                raise RuntimeError(f"浏览器验证同步失败：{str(exc)[:160]}") from exc

    def complete(self, shop_id: int) -> dict[str, Any]:
        with self.lock:
            if self.driver is None or self.shop_id != shop_id:
                raise RuntimeError("没有等待完成的浏览器验证会话")
            driver = self.driver
            with database() as connection:
                shop = connection.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
            if shop is None:
                self._close()
                raise KeyError("监控店铺不存在")
            try:
                source = driver.page_source
                if self._is_waf_html(source):
                    return {
                        "status": "awaiting_verification",
                        "detail": "滑块验证尚未完成，请在 Edge 窗口完成后重试",
                    }
                driver.get(shop["url"])
                first_payload = self._browser_request(driver, self._request_data(shop, 1))
                products = self._catalog(driver, shop, first_payload)
                with WORKER.fetch_lock:
                    summary = record_shop_fetch(shop_id, products_override=products)
                self._close()
                return {"status": "success", "summary": summary}
            except WafChallengeRequired as exc:
                return self._render_challenge(driver, str(exc))
            except Exception as exc:
                self._close()
                raise RuntimeError(f"浏览器验证同步失败：{str(exc)[:160]}") from exc


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
        if path == "/api/sub2api/config":
            return self._send_json(sub2api_settings())
        if path == "/api/sub2api/automation":
            return self._send_json({
                "ok": True,
                "settings": sub2api_automation_settings(),
                "state": sub2api_automation_state(),
            })
        if path == "/api/sub2api/options":
            try:
                return self._send_json(fetch_sub2api_options())
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)
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

        if path == "/api/sub2api/test":
            config = sub2api_settings(reveal=True)
            if not config["admin_key"]:
                return self._send_json({"detail": "请先配置 Sub2API 管理员密钥"}, 400)
            try:
                status, payload, _ = _external_json_request(
                    "GET",
                    config["base_url"] + "/api/v1/admin/accounts",
                    # Sub2API's admin middleware authenticates API keys via x-api-key.
                    # Authorization: Bearer is reserved for admin JWTs.
                    headers={"x-api-key": config["admin_key"]},
                    timeout=15,
                )
            except (ValueError, RuntimeError) as exc:
                return self._send_json({"detail": str(exc)}, 502)
            accounts = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(accounts, dict):
                accounts = accounts.get("accounts") or accounts.get("items") or accounts.get("list")
            count = len(accounts) if isinstance(accounts, list) else None
            return self._send_json({"ok": 200 <= status < 300, "upstream_status": status, "account_count": count})

        if path in ("/api/sub2api/reclaim-401", "/api/sub2api/reclaim401"):
            try:
                result = reclaim_sub2api_401_accounts()
                if not isinstance(result, dict):
                    return self._send_json({"detail": "401 找回返回格式无效"}, 502)
                if not result.get("ok", False):
                    failure = result.get("result") if isinstance(result.get("result"), dict) else {}
                    detail = str(failure.get("error") or result.get("error") or "401 找回服务返回失败")[:240]
                    result = {**result, "detail": detail}
                return self._send_json(result, 200 if result.get("ok", False) else 502)
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)

        if path == "/api/sub2api/reclaim-progress":
            raw_codes = data.get("card_codes")
            if not isinstance(raw_codes, list):
                return self._send_json({"detail": "请提供 card_codes 数组"}, 400)
            try:
                result = refresh_sub2api_reclaim(raw_codes)
                return self._send_json(result, 200 if result["ok"] else 502)
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)

        if path == "/api/sub2api/automation/run":
            result = AUTOMATION_WORKER.run_once()
            return self._send_json(result, 200 if result.get("ok", False) else 502)

        if path == "/api/sub2api/import":
            try:
                source = data.get("data") if "data" in data else data
                result = _sub2api_import_payload(
                    source,
                    proxy_id=data.get("proxy_id"),
                    group_ids=data.get("group_ids"),
                    codex_fingerprint_mode=data.get("codex_fingerprint_mode") if "codex_fingerprint_mode" in data else None,
                    assign_existing=data.get("assign_existing") if "assign_existing" in data else None,
                    endpoint=str(data.get("endpoint") or "/api/v1/admin/accounts/data"),
                )
                return self._send_json(result)
            except (TypeError, ValueError) as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)

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
                interval = normalize_interval(data.get("interval_seconds"), 300)
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
                    product = serialize_snapshot(latest)
                    if product["sale_status"] != "on_sale":
                        return self._send_json({"detail": f"{product['title']} 当前不是在售状态"}, 409)
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
                if latest is None:
                    raise ValueError("商品尚未完成有效同步")
                product = serialize_snapshot(latest)
                if product["sale_status"] != "on_sale":
                    raise ValueError("商品当前已下架")
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
                enabled = 1 if bool(data.get("enabled", True)) else 0
                interval = normalize_interval(data.get("interval_seconds"))
                name = str(data.get("name") or "").strip()[:100]
            except (TypeError, ValueError):
                return self._send_json({"detail": "监控设置无效"}, 400)
            with database() as connection:
                cursor = connection.execute(
                    "UPDATE watches SET name = ?, enabled = ?, interval_seconds = ? WHERE id = ?",
                    (name, enabled, interval, watch_id),
                )
            if cursor.rowcount == 0:
                return self._send_json({"detail": "监控商品不存在"}, 404)
            return self._send_json({"ok": True})

        shop_match = re.fullmatch(r"/api/shops/(\d+)", path)
        if shop_match:
            shop_id = int(shop_match.group(1))
            try:
                enabled = 1 if bool(data.get("enabled", True)) else 0
                interval = normalize_interval(data.get("interval_seconds"), 300)
                name = str(data.get("name") or "").strip()[:100]
                keywords = str(data.get("keywords") or "").strip()[:100]
                raw_category = data.get("category_id")
                category_id = int(raw_category) if raw_category not in (None, "") else None
                goods_type = str(data.get("goods_type") or "card").strip()[:30]
            except (TypeError, ValueError):
                return self._send_json({"detail": "店铺监控设置无效"}, 400)
            with database() as connection:
                cursor = connection.execute(
                    """
                    UPDATE shops SET name = ?, keywords = ?, category_id = ?, goods_type = ?,
                        enabled = ?, interval_seconds = ? WHERE id = ?
                    """,
                    (name, keywords, category_id, goods_type, enabled, interval, shop_id),
                )
            if cursor.rowcount == 0:
                return self._send_json({"detail": "监控店铺不存在"}, 404)
            return self._send_json({"ok": True})

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

        if path == "/api/sub2api/config":
            try:
                base_url = _normalize_service_url(data.get("base_url"), DEFAULT_SUB2API_URL)
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            supplied_key = data.get("admin_key")
            current = sub2api_settings(reveal=True)
            if supplied_key is None or str(supplied_key).strip() == "":
                admin_key = current["admin_key"]
            else:
                admin_key = str(supplied_key).strip()
            if len(admin_key) > 512:
                return self._send_json({"detail": "管理员密钥过长"}, 400)
            _store_setting("sub2api", {"base_url": base_url, "admin_key": admin_key})
            return self._send_json(sub2api_settings())

        if path == "/api/sub2api/automation":
            try:
                settings = save_sub2api_automation_settings(data)
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            return self._send_json({"ok": True, "settings": settings, "state": sub2api_automation_state()})

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
