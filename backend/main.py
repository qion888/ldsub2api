from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
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
from monitor_core.inventory import InventoryService
from monitor_core import preorders as preorder_service
from monitor_core import settings as monitor_setting_store
from monitor_core import storefront
from monitor_core.browser_verification import BrowserVerificationManager as CoreBrowserVerificationManager
from monitor_core.workers import MonitorWorker as CoreMonitorWorker
from order_query.complaint import build_complaint_preview
from order_query.browser_verification import OrderQueryBrowserVerificationManager
from order_query import routes as order_query_routes
from order_query.service import OrderQueryService
from sub2api import automation as sub2api_automation
from sub2api import card_import_history as sub2api_card_history
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
ALLOWED_HOST = storefront.ALLOWED_HOST
MAX_RESPONSE_BYTES = storefront.MAX_RESPONSE_BYTES
USER_AGENT = storefront.USER_AGENT
VISITOR_ID_PATTERN = storefront.VISITOR_ID_PATTERN
WAF_MARKERS = storefront.WAF_MARKERS
UNLISTED_ERROR_MARKERS = storefront.UNLISTED_ERROR_MARKERS
DEFAULT_REDEEM_URL = "https://30d.team"
DEFAULT_SUB2API_URL = DEFAULT_URL
DEFAULT_SUB2API_AUTOMATION = DEFAULT_AUTOMATION
MAX_EXTERNAL_JSON_BYTES = 8 * 1024 * 1024
MAX_REQUEST_JSON_BYTES = 32 * 1024 * 1024


WafChallengeRequired = storefront.WafChallengeRequired


PreorderConflict = preorder_service.PreorderConflict


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
    sub2api_card_history.initialize(database)


DescriptionParser = storefront.DescriptionParser


def plain_text(value: Any) -> str:
    return storefront.plain_text(value)


def parse_item_url(value: str) -> tuple[str, str]:
    return storefront.parse_item_url(value)


def parse_shop_url(value: str) -> tuple[str, str]:
    return storefront.parse_shop_url(value)


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return storefront._first_value(item, keys)


def _stock_value(item: dict[str, Any]) -> Any:
    return storefront._stock_value(item)


def _money(value: Any) -> str:
    return storefront._money(value)


def _compact_number(value: Any) -> str:
    return storefront._compact_number(value)


def _upstream_enabled(value: Any) -> bool:
    return storefront._upstream_enabled(value)


def commerce_tags_from_goods(item: dict[str, Any]) -> list[dict[str, str]]:
    return storefront.commerce_tags_from_goods(item)


def is_unlisted_error(value: Any) -> bool:
    return storefront.is_unlisted_error(value)


def normalize_goods_payload(payload: dict[str, Any], goods_key: str) -> dict[str, Any]:
    return storefront.normalize_goods_payload(payload, goods_key)


def _post_shop_api(
    endpoint: str,
    payload: dict[str, Any],
    referer: str,
    *,
    visitor_id: str | None = None,
) -> dict[str, Any]:
    return storefront._post_shop_api(
        endpoint,
        payload,
        referer,
        visitor_id=visitor_id,
        opener=urlopen,
    )


def _random_visitor_id() -> str:
    return storefront._random_visitor_id()


def fetch_buyer_juuid(shop_token: str) -> dict[str, Any]:
    return storefront.fetch_buyer_juuid(shop_token, opener=urlopen)


def fetch_payment_channels(shop_token: str) -> list[dict[str, Any]]:
    return storefront.fetch_payment_channels(
        shop_token,
        post_api=_post_shop_api,
        visitor_id_factory=_random_visitor_id,
    )


def fetch_shop_categories(shop_url: str, *, goods_type: str = "card") -> dict[str, Any]:
    return storefront.fetch_shop_categories(
        shop_url,
        goods_type=goods_type,
        post_api=_post_shop_api,
        visitor_id=_random_visitor_id(),
    )


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
    return storefront.create_official_payment_order(
        goods_key=goods_key,
        quantity=quantity,
        coupon_code=coupon_code,
        channel_id=channel_id,
        contact=contact,
        query_password=query_password,
        select_cards_ids=select_cards_ids,
        juuid=juuid,
        referer=referer,
        visitor_id=visitor_id,
        post_api=_post_shop_api,
        visitor_id_factory=_random_visitor_id,
    )


def fetch_goods(url: str) -> dict[str, Any]:
    return storefront.fetch_goods(url, post_api=_post_shop_api)


def _goods_list_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return storefront._goods_list_rows(payload)


def normalize_goods_list_item(item: dict[str, Any], shop_token: str) -> dict[str, Any]:
    return storefront.normalize_goods_list_item(item, shop_token)


def fetch_shop_catalog(
    shop_url: str,
    *,
    keywords: str = "",
    category_id: int | None = None,
    goods_type: str = "card",
    page_size: int = 50,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    return storefront.fetch_shop_catalog(
        shop_url,
        keywords=keywords,
        category_id=category_id,
        goods_type=goods_type,
        page_size=page_size,
        max_pages=max_pages,
        post_api=_post_shop_api,
    )




INVENTORY = InventoryService(
    database=lambda: database(),
    now=lambda: utc_now(),
    fetch_goods=lambda url: fetch_goods(url),
    fetch_shop_catalog=lambda url, **kwargs: fetch_shop_catalog(url, **kwargs),
    commerce_tags=lambda item: commerce_tags_from_goods(item),
    is_unlisted_error=lambda value: is_unlisted_error(value),
    sync_intervals=sync_shop_product_intervals,
)


def _json_value(value: str | None, fallback: Any) -> Any:
    return INVENTORY._json_value(value, fallback)


def serialize_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return INVENTORY.serialize_snapshot(row)


def effective_watch_product(
    connection: sqlite3.Connection,
    watch_id: int,
    latest: sqlite3.Row | None = None,
    attempt: sqlite3.Row | None = None,
) -> dict[str, Any] | None:
    return INVENTORY.effective_watch_product(connection, watch_id, latest, attempt)


def record_fetch(watch_id: int) -> dict[str, Any]:
    return INVENTORY.record_fetch(watch_id)


def record_shop_fetch(
    shop_id: int,
    products_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return INVENTORY.record_shop_fetch(shop_id, products_override)


def _watch_inventory_shop_id(watch_id: int) -> int | None:
    return INVENTORY._watch_inventory_shop_id(watch_id)


def _latest_watch_product(
    watch_id: int,
    *,
    shop_id: int,
    shop_summary: dict[str, Any],
) -> dict[str, Any]:
    return INVENTORY._latest_watch_product(
        watch_id,
        shop_id=shop_id,
        shop_summary=shop_summary,
    )


def record_inventory_fetch(
    watch_id: int,
    shop_refresh_cache: dict[int, dict[str, Any] | Exception] | None = None,
) -> dict[str, Any]:
    return INVENTORY.record_inventory_fetch(watch_id, shop_refresh_cache)


def list_shops() -> list[dict[str, Any]]:
    return INVENTORY.list_shops()


def list_watches() -> list[dict[str, Any]]:
    return INVENTORY.list_watches()




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


def list_sub2api_card_import_records(query_values: dict[str, list[str]]) -> dict[str, Any]:
    return sub2api_card_history.list_records(database, query_values)


def create_sub2api_card_import_record(payload: dict[str, Any]) -> dict[str, Any]:
    return sub2api_card_history.create_record(database, payload, now=utc_now)


def update_sub2api_card_import_record(record_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return sub2api_card_history.update_record(database, record_id, payload, now=utc_now)


def retry_sub2api_card_import_record(record_id: int) -> dict[str, Any]:
    return sub2api_card_history.retry_record(
        database,
        record_id,
        import_payload=_sub2api_import_payload,
        now=utc_now,
    )


def delete_sub2api_card_import_record(record_id: int) -> dict[str, Any]:
    return sub2api_card_history.delete_record(database, record_id)


def delete_sub2api_card_import_records(record_ids: list[int]) -> dict[str, Any]:
    return sub2api_card_history.delete_records(database, record_ids)


def _sub2api_imported_order_nos() -> list[str]:
    try:
        value = sub2api_automation_state().get("imported_order_nos", [])
    except sqlite3.OperationalError:
        # Standalone client operations may run before the local database is
        # initialized (for example during a health check or unit test).
        return []
    return [str(item).strip() for item in value if str(item).strip()][:500]


def _store_sub2api_automation_state(value: dict[str, Any]) -> None:
    _store_setting("sub2api_automation_state", value)


def _sub2api_reclaim_attempt_context() -> tuple[dict[str, int], int, list[str]]:
    try:
        settings = sub2api_automation_settings()
        state = sub2api_automation_state()
    except sqlite3.OperationalError:
        settings = {"max_reclaim_attempts": 3}
        state = {}
    attempts = state.get("reclaim_attempts") if isinstance(state.get("reclaim_attempts"), dict) else {}
    normalized = {
        str(code).strip(): max(0, int(count))
        for code, count in attempts.items()
        if str(code).strip() and str(count).strip().lstrip("-").isdigit()
    }
    try:
        maximum = min(max(int(settings.get("max_reclaim_attempts") or 3), 1), 20)
    except (TypeError, ValueError):
        maximum = 3
    limited = [
        str(value).strip()
        for value in state.get("attempt_limited_card_codes", [])
        if str(value).strip()
    ]
    limited = list(dict.fromkeys(limited + [code for code, count in normalized.items() if count >= maximum]))
    return normalized, maximum, limited


def _sub2api_reclaim_call_context(
    *,
    attempt_counts: dict[str, int] | None,
    max_reclaim_attempts: int | None,
    exclude_card_codes: list[str] | None,
) -> tuple[dict[str, int], int, list[str]]:
    persisted_attempts, persisted_maximum, persisted_limited = _sub2api_reclaim_attempt_context()
    if isinstance(attempt_counts, dict):
        normalized_attempts = {
            str(code).strip(): max(0, int(count))
            for code, count in attempt_counts.items()
            if str(code).strip()
            and str(count).strip().lstrip("-").isdigit()
        }
    else:
        normalized_attempts = persisted_attempts
    if max_reclaim_attempts is None:
        maximum = persisted_maximum
    else:
        try:
            maximum = min(max(int(max_reclaim_attempts), 1), 20)
        except (TypeError, ValueError):
            maximum = persisted_maximum
    if isinstance(exclude_card_codes, list):
        limited = list(dict.fromkeys(
            str(code).strip() for code in exclude_card_codes if str(code).strip()
        ))
    else:
        limited = persisted_limited
    limited = list(dict.fromkeys(
        limited + [code for code, count in normalized_attempts.items() if count >= maximum]
    ))
    return normalized_attempts, maximum, limited


def _persist_sub2api_reclaim_attempts(result: dict[str, Any]) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("attempt_counts"), dict):
        return
    try:
        state = sub2api_automation_state()
    except sqlite3.OperationalError:
        return
    attempts = {
        str(code).strip(): max(0, int(count))
        for code, count in result["attempt_counts"].items()
        if str(code).strip() and str(count).strip().lstrip("-").isdigit()
    }
    limited = state.get("attempt_limited_card_codes") if isinstance(state.get("attempt_limited_card_codes"), list) else []
    limited.extend(result.get("attempt_limited_card_codes") if isinstance(result.get("attempt_limited_card_codes"), list) else [])
    state = {
        **state,
        "reclaim_attempts": attempts,
        "attempt_limited_card_codes": list(dict.fromkeys(str(code).strip() for code in limited if str(code).strip()))[:100],
    }
    _store_sub2api_automation_state(state)


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


def fetch_sub2api_account_page(query_values: dict[str, list[str]] | None = None) -> dict[str, Any]:
    query_values = query_values or {}

    def first(name: str, default: str = "") -> str:
        value = query_values.get(name, [default])
        return str(value[0] if isinstance(value, list) and value else default)

    try:
        page = int(first("page", "1"))
        page_size = int(first("page_size", "12"))
    except ValueError as exc:
        raise ValueError("璐﹀彿鍒嗛〉鍙傛暟鏃犳晥") from exc
    return sub2api_client.fetch_account_page(
        sub2api_settings(reveal=True),
        request_json=_external_json_request,
        page=page,
        page_size=page_size,
        search=first("search")[:100],
        status_filter=first("status")[:40],
        platform=first("platform")[:40],
    )


def test_sub2api_account(account_id: int) -> dict[str, Any]:
    return sub2api_client.test_account(
        sub2api_settings(reveal=True),
        int(account_id),
        request_json=_external_json_request,
    )


def delete_sub2api_account(account_id: int) -> dict[str, Any]:
    return sub2api_client.delete_account(
        sub2api_settings(reveal=True),
        int(account_id),
        request_json=_external_json_request,
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
    *, include_downloads: bool = True, exclude_order_nos: list[str] | None = None,
    attempt_counts: dict[str, int] | None = None,
    max_reclaim_attempts: int | None = None,
    exclude_card_codes: list[str] | None = None,
) -> dict[str, Any]:
    if exclude_order_nos is None:
        exclude_order_nos = _sub2api_imported_order_nos()
    attempts, maximum, limited = _sub2api_reclaim_call_context(
        attempt_counts=attempt_counts,
        max_reclaim_attempts=max_reclaim_attempts,
        exclude_card_codes=exclude_card_codes,
    )
    result = sub2api_reclaim.reclaim_401_accounts(
        config=sub2api_settings(reveal=True),
        accounts_loader=_sub2api_fetch_accounts,
        redeem_client_factory=_redeem_client,
        to_json=_dataclass_to_json,
        max_payload_bytes=MAX_EXTERNAL_JSON_BYTES,
        include_downloads=include_downloads,
        exclude_order_nos=exclude_order_nos,
        attempt_counts=attempts,
        max_reclaim_attempts=maximum,
        exclude_card_codes=limited,
    )
    _persist_sub2api_reclaim_attempts(result)
    return result


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
    reclaim_order_nos: list[str] | None = None,
) -> dict[str, Any]:
    if reclaim_order_nos is not None:
        if not isinstance(reclaim_order_nos, list):
            raise ValueError("reclaim_order_nos 必须是数组")
        reclaim_order_nos = list(dict.fromkeys(
            value
            for value in (str(item).strip() for item in reclaim_order_nos[:100])
            if re.fullmatch(r"[A-Za-z0-9_-]{3,160}", value)
        ))
    result = sub2api_client.import_payload(
        source,
        config=sub2api_settings(reveal=True),
        request_json=_external_json_request,
        fingerprint_reconciler=_sub2api_reconcile_codex_fingerprint,
        proxy_id=proxy_id,
        group_ids=group_ids,
        codex_fingerprint_mode=codex_fingerprint_mode,
        assign_existing=assign_existing,
        endpoint=endpoint,
        accounts_loader=_sub2api_fetch_accounts,
    )
    if reclaim_order_nos and result.get("import_verification", {}).get("confirmed"):
        state = sub2api_automation_state()
        existing = [str(value).strip() for value in state.get("imported_order_nos", []) if str(value).strip()]
        incoming = [str(value).strip() for value in reclaim_order_nos if str(value).strip()][:100]
        state["imported_order_nos"] = list(dict.fromkeys(existing + incoming))[-500:]
        _store_sub2api_automation_state(state)
    return result


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
    card_codes: list[str], *, exclude_order_nos: list[str] | None = None,
    include_downloads: bool = True,
) -> dict[str, Any]:
    if exclude_order_nos is None:
        exclude_order_nos = _sub2api_imported_order_nos()
    return sub2api_reclaim.refresh_reclaim(
        card_codes,
        redeem_client_factory=_redeem_client,
        to_json=_dataclass_to_json,
        max_payload_bytes=MAX_EXTERNAL_JSON_BYTES,
        exclude_order_nos=exclude_order_nos,
        include_downloads=include_downloads,
    )


def retry_sub2api_401_accounts(
    card_codes: list[str], *, exclude_order_nos: list[str] | None = None,
    include_downloads: bool = True, attempt_counts: dict[str, int] | None = None,
    max_reclaim_attempts: int | None = None,
    exclude_card_codes: list[str] | None = None,
) -> dict[str, Any]:
    if exclude_order_nos is None:
        exclude_order_nos = _sub2api_imported_order_nos()
    attempts, maximum, limited = _sub2api_reclaim_call_context(
        attempt_counts=attempt_counts,
        max_reclaim_attempts=max_reclaim_attempts,
        exclude_card_codes=exclude_card_codes,
    )
    result = sub2api_reclaim.retry_reclaim(
        card_codes,
        redeem_client_factory=_redeem_client,
        to_json=_dataclass_to_json,
        max_payload_bytes=MAX_EXTERNAL_JSON_BYTES,
        exclude_order_nos=exclude_order_nos,
        include_downloads=include_downloads,
        attempt_counts=attempts,
        max_reclaim_attempts=maximum,
        exclude_card_codes=limited,
    )
    _persist_sub2api_reclaim_attempts(result)
    return result


def persist_sub2api_automation_retry(result: dict[str, Any]) -> dict[str, Any]:
    """Persist an explicit automation retry without storing downloaded blobs."""
    if not isinstance(result, dict):
        return result
    state = sub2api_automation_state()
    reclaim_summary = result.get("reclaim_summary")
    reclaim_summary = reclaim_summary if isinstance(reclaim_summary, dict) else {}
    raw_retry_codes = result.get("retryable_card_codes")
    raw_retry_codes = raw_retry_codes if isinstance(raw_retry_codes, list) else []
    raw_permanent_codes = []
    for key in (
        "permanent_card_codes",
        "unrecoverable_card_codes",
        "non_retryable_card_codes",
        "not_owned_card_codes",
        "skipped_card_codes",
    ):
        values = result.get(key)
        if isinstance(values, list):
            raw_permanent_codes.extend(values)
    permanent_codes = {
        str(value).strip() for value in raw_permanent_codes if str(value).strip()
    }
    retry_codes = [
        str(value).strip()
        for value in raw_retry_codes
        if str(value).strip() and str(value).strip() not in permanent_codes
    ][:100]
    attempt_counts = result.get("attempt_counts") if isinstance(result.get("attempt_counts"), dict) else state.get("reclaim_attempts", {})
    attempt_counts = {
        str(code).strip(): max(0, int(count))
        for code, count in attempt_counts.items()
        if str(code).strip() and str(count).strip().lstrip("-").isdigit()
    }
    limited_codes = state.get("attempt_limited_card_codes") if isinstance(state.get("attempt_limited_card_codes"), list) else []
    limited_codes.extend(result.get("attempt_limited_card_codes") if isinstance(result.get("attempt_limited_card_codes"), list) else [])
    limited_codes = list(dict.fromkeys(str(code).strip() for code in limited_codes if str(code).strip()))[:100]
    retry_codes = [code for code in retry_codes if code not in limited_codes]
    raw_submitted_codes = result.get("reclaim_card_codes")
    raw_submitted_codes = raw_submitted_codes if isinstance(raw_submitted_codes, list) else []
    submitted_codes = [
        str(value).strip()
        for value in raw_submitted_codes
        if str(value).strip()
    ][:100]
    raw_active_codes = result.get("active_card_codes")
    raw_active_codes = raw_active_codes if isinstance(raw_active_codes, list) else []
    active_codes = [
        str(value).strip()
        for value in raw_active_codes
        if str(value).strip()
        and str(value).strip() not in permanent_codes
        and str(value).strip() not in limited_codes
    ][:100]
    def _reclaim_count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    active = _reclaim_count(reclaim_summary.get("active"))
    outcome = str(result.get("outcome") or result.get("recovery_status") or "error")
    pending = list(dict.fromkeys(active_codes + retry_codes)) if active > 0 else retry_codes
    permanent_count = sum(
        _reclaim_count(reclaim_summary.get(key))
        for key in ("unreclaimable", "not_owned", "skipped")
    )
    if active > 0 and not pending and not permanent_count:
        pending = submitted_codes
    downloads = result.get("downloaded_payloads")
    if isinstance(downloads, list):
        download_count = len(downloads)
    else:
        try:
            download_count = max(0, int(result.get("downloaded") or 0))
        except (TypeError, ValueError):
            download_count = 0
    compact_result = {
        "ok": bool(result.get("ok", False)),
        "outcome": outcome,
        "recovery_status": result.get("recovery_status", outcome),
        "recovery_ok": bool(result.get("recovery_ok", outcome in {"recovered", "no_401"})),
        "recovery_message": str(result.get("recovery_message") or result.get("detail") or result.get("error") or "")[:500],
        "downloads_requested": bool(result.get("downloads_requested", True)),
        "scanned_accounts": result.get("scanned_accounts"),
        "accounts_401": result.get("accounts_401"),
        "card_code_count": result.get("card_code_count", len(submitted_codes)),
        "queued": reclaim_summary.get("queued", 0),
        "already_running": reclaim_summary.get("already_running", 0),
        "active": active,
        "active_card_codes": active_codes,
        "done": reclaim_summary.get("done", 0),
        "failed": reclaim_summary.get("failed", result.get("failed", 0)),
        "unreclaimable": reclaim_summary.get("unreclaimable", result.get("unreclaimable", 0)),
        "not_owned": reclaim_summary.get("not_owned", result.get("not_owned", 0)),
        "skipped": reclaim_summary.get("skipped", result.get("skipped", 0)),
        "downloaded": download_count,
        "download_failed": reclaim_summary.get("download_failed", result.get("download_failed", 0)),
        "download_skipped": reclaim_summary.get("download_skipped", result.get("download_skipped", 0)),
        "reclaim_summary": reclaim_summary,
        "reclaim_failures": result.get("reclaim_failures") if isinstance(result.get("reclaim_failures"), list) else [],
        "retryable_card_codes": retry_codes,
        "attempt_counts": attempt_counts,
        "attempt_limited_card_codes": limited_codes,
        "retry_available": bool(result.get("retry_available", bool(retry_codes)) and retry_codes),
        "import_status": result.get("import_status", "not_attempted"),
        "import_error": str(result.get("import_error") or "")[:500],
        "import_attempted": bool(result.get("import_attempted", False)),
        "imported": bool(result.get("imported", False)),
        "import_result": result.get("import_result") if isinstance(result.get("import_result"), dict) else None,
    }
    state = {
        **state,
        "last_run": utc_now(),
        "last_error": "" if compact_result["ok"] else compact_result["recovery_message"],
        "last_result": compact_result,
        "pending_card_codes": pending,
        "retryable_card_codes": retry_codes,
        "reclaim_attempts": attempt_counts,
        "attempt_limited_card_codes": limited_codes,
        "run_history": (
            state.get("run_history", [])
            + [{
                "run_at": utc_now(),
                "status": "error" if not compact_result["ok"] else (
                    "success" if outcome in {"recovered", "no_401"} else "partial"
                ),
                **compact_result,
            }]
        )[-20:],
    }
    _store_sub2api_automation_state(state)
    return result


def run_sub2api_automation_cycle() -> dict[str, Any]:
    return sub2api_automation.run_cycle(
        settings_loader=sub2api_automation_settings,
        state_loader=sub2api_automation_state,
        refresh_reclaim=refresh_sub2api_reclaim,
        reclaim_accounts=reclaim_sub2api_401_accounts,
        retry_reclaim=retry_sub2api_401_accounts,
        import_payload=_sub2api_import_payload,
        store_state=_store_sub2api_automation_state,
        now=utc_now,
    )


def list_preorders() -> list[dict[str, Any]]:
    return preorder_service.list_preorders(
        database=database,
        effective_product=effective_watch_product,
    )


def create_preorders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return preorder_service.create_preorders(
        payload,
        database=database,
        settings_loader=checkout_settings,
        interval_normalizer=normalize_interval,
        now=utc_now,
        effective_product=effective_watch_product,
        list_loader=list_preorders,
    )


def process_preorder(preorder_id: int, product: dict[str, Any]) -> dict[str, Any] | None:
    return preorder_service.process_preorder(
        preorder_id,
        product,
        database=database,
        now=utc_now,
        identity_loader=fetch_buyer_juuid,
        order_creator=create_official_payment_order,
    )


def mark_preorder_check_error(preorder_id: int, error: Exception) -> None:
    preorder_service.mark_check_error(
        preorder_id,
        error,
        database=database,
        now=utc_now,
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
    return INVENTORY.delete_watches(watch_ids)


def delete_shops(shop_ids: list[int]) -> int:
    return INVENTORY.delete_shops(shop_ids)


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
ORDER_QUERY_SERVICE = OrderQueryService()
ORDER_QUERY_BROWSER_VERIFICATION = OrderQueryBrowserVerificationManager(
    sessions=ORDER_QUERY_SERVICE.sessions,
    waf_markers=WAF_MARKERS,
    profile_path=Path(__file__).with_name("order-waf-browser-profile"),
)


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
                "sub2api_accounts": True,
                "sub2api_card_import_history": True,
                "sub2api_card_import_history_delete": True,
                "order_query": True,
                "order_query_waf_verification": True,
                "order_complaint_submit": True,
                "order_complaint_history": True,
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
            account_loader=fetch_sub2api_account_page,
            card_import_history_loader=list_sub2api_card_import_records,
            query_values=parse_qs(parsed.query),
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
        rejection = order_query_routes.request_rejection(
            path,
            self.headers,
            frontend_url=FRONTEND_URL,
        )
        if rejection is not None:
            status, payload = rejection
            return self._send_json(payload, status)
        try:
            data = self._read_json()
        except ValueError as exc:
            return self._send_json({"detail": str(exc)}, 400)

        if order_query_routes.handle_post(
            path,
            data,
            send_json=self._send_json,
            search=ORDER_QUERY_SERVICE.search,
            detail=ORDER_QUERY_SERVICE.detail,
            complaint_preview=build_complaint_preview,
            complaint_context=getattr(ORDER_QUERY_SERVICE, "complaint_context", None),
            complaint_history=getattr(ORDER_QUERY_SERVICE, "complaint_history", None),
            complaint_upload=getattr(ORDER_QUERY_SERVICE, "complaint_upload", None),
            complaint_submit=getattr(ORDER_QUERY_SERVICE, "complaint_submit", None),
            complaint_remove_upload=getattr(ORDER_QUERY_SERVICE, "complaint_remove_upload", None),
        ):
            return

        if path in (order_query_routes.ORDER_WAF_START_PATH, order_query_routes.ORDER_WAF_COMPLETE_PATH):
            try:
                result = (
                    ORDER_QUERY_BROWSER_VERIFICATION.start(data)
                    if path == order_query_routes.ORDER_WAF_START_PATH
                    else ORDER_QUERY_BROWSER_VERIFICATION.complete(data)
                )
                return self._send_json(result, 202 if result.get("status") == "awaiting_verification" else 200)
            except order_query_routes.OrderQueryError as exc:
                return self._send_json({"detail": exc.detail, "code": exc.code, "retryable": exc.retryable}, exc.status)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)

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
            test_account=test_sub2api_account,
            card_import_history_creator=create_sub2api_card_import_record,
            card_import_history_retry=retry_sub2api_card_import_record,
            card_import_history_deleter=delete_sub2api_card_import_record,
            card_import_history_batch_deleter=delete_sub2api_card_import_records,
            retry_reclaim=retry_sub2api_401_accounts,
            persist_automation_retry=persist_sub2api_automation_retry,
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

        if path == "/api/shops/categories":
            try:
                result = fetch_shop_categories(
                    str(data.get("url") or ""),
                    goods_type=str(data.get("goods_type") or "card"),
                )
            except ValueError as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except WafChallengeRequired as exc:
                return self._send_json({"detail": str(exc)}, 409)
            except RuntimeError as exc:
                return self._send_json({"detail": str(exc)}, 502)
            return self._send_json(result)

        if path == "/api/shops/batch-delete":
            raw_ids = data.get("ids")
            if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 500:
                return self._send_json({"detail": "请选择 1 到 500 个店铺"}, 400)
            try:
                shop_ids = sorted({int(value) for value in raw_ids})
            except (TypeError, ValueError):
                return self._send_json({"detail": "店铺编号无效"}, 400)
            if any(value < 1 for value in shop_ids):
                return self._send_json({"detail": "店铺编号无效"}, 400)
            deleted_count = delete_shops(shop_ids)
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
                category_name = str(data.get("category_name") or "").strip()[:100] if category_id else ""
                goods_type = str(data.get("goods_type") or "card").strip()[:30]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", goods_type):
                    raise ValueError("商品类型格式无效")
                with database() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO shops(
                            url, token, name, keywords, category_id, category_name, goods_type,
                            enabled, interval_seconds, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (canonical_url, token, name, keywords, category_id, category_name, goods_type, interval, utc_now()),
                    )
                    shop_id = cursor.lastrowid
            except (TypeError, ValueError) as exc:
                return self._send_json({"detail": str(exc)}, 400)
            except sqlite3.IntegrityError:
                with database() as connection:
                    existing = connection.execute(
                        "SELECT id, name, category_id, category_name FROM shops WHERE token = ? OR url = ?",
                        (token, canonical_url),
                    ).fetchone()
                    if existing is None:
                        return self._send_json({"detail": "店铺配置冲突"}, 409)
                    shop_id = existing["id"]
                    if not category_name and existing["category_id"] == category_id:
                        category_name = str(existing["category_name"] or "")
                    connection.execute(
                        """
                        UPDATE shops SET name = ?, keywords = ?, category_id = ?, category_name = ?, goods_type = ?,
                            enabled = 1, interval_seconds = ? WHERE id = ?
                        """,
                        (
                            requested_name or existing["name"] or token,
                            keywords,
                            category_id,
                            category_name,
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
                            "coupon_supported": any(tag.get("key") == "coupon" for tag in product.get("commerce_tags", [])),
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
            coupon_code = str(data.get("coupon_code") or "").strip()[:120]
            storage_mode = str(data.get("storage_mode") or "local").strip().lower()
            if storage_mode not in {"local", "browser"}:
                return self._send_json({"detail": "存储方式无效"}, 400)
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
                "coupon_code": coupon_code,
                "storage_mode": storage_mode,
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
            card_import_history_updater=update_sub2api_card_import_record,
        ):
            return

        return self._send_json({"detail": "接口不存在"}, 404)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if sub2api_routes.handle_delete(
            path,
            send_json=self._send_json,
            delete_account=delete_sub2api_account,
            card_import_history_deleter=delete_sub2api_card_import_record,
        ):
            return
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
            if delete_shops([int(shop_match.group(1))]) == 0:
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
        ORDER_QUERY_BROWSER_VERIFICATION._close()
        ORDER_QUERY_SERVICE.sessions.clear()
        server.server_close()


if __name__ == "__main__":
    run()
