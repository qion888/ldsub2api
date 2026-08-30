"""HTTP route adapter for order lookup."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

from monitor_core.storefront import WafChallengeRequired

from .errors import OrderQueryError

SendJson = Callable[[Any, int], Any]
ORDER_QUERY_PATH = "/api/order-query/search"
ORDER_DETAIL_PATH = "/api/order-query/detail"
ORDER_QUERY_PATHS = {ORDER_QUERY_PATH, ORDER_DETAIL_PATH}
LOCAL_DEVELOPMENT_HOSTS = {"127.0.0.1", "localhost"}


def _origin_key(value: str) -> tuple[str, str, int] | None:
    parsed = urlparse(str(value or "").strip())
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    scheme = parsed.scheme.lower()
    return scheme, parsed.hostname.lower(), port or (443 if scheme == "https" else 80)


def request_rejection(
    path: str,
    headers: Any,
    *,
    frontend_url: str,
) -> tuple[int, dict[str, Any]] | None:
    """Reject browser requests that could make the local service perform cross-site work."""
    if path not in ORDER_QUERY_PATHS:
        return None

    content_type = str(headers.get("Content-Type", "")).partition(";")[0].strip().lower()
    if content_type != "application/json":
        return 415, {
            "detail": "订单查询仅接受 application/json 请求",
            "code": "unsupported_media_type",
            "retryable": False,
        }

    origin = str(headers.get("Origin", "")).strip()
    if not origin:
        return None
    parsed_origin = urlparse(origin)
    origin_key = _origin_key(origin)
    frontend_key = _origin_key(frontend_url)
    is_bare_origin = (
        parsed_origin.path in {"", "/"}
        and not parsed_origin.params
        and not parsed_origin.query
        and not parsed_origin.fragment
    )
    is_frontend = origin_key is not None and origin_key == frontend_key
    is_local_development = (
        origin_key is not None
        and origin_key[0] == "http"
        and origin_key[1] in LOCAL_DEVELOPMENT_HOSTS
    )
    if is_bare_origin and (is_frontend or is_local_development):
        return None
    return 403, {
        "detail": "订单查询请求来源不受信任",
        "code": "untrusted_origin",
        "retryable": False,
    }


def handle_post(
    path: str,
    data: dict[str, Any],
    *,
    send_json: SendJson,
    search: Callable[[dict[str, Any]], dict[str, Any]],
    detail: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    if path == ORDER_QUERY_PATH:
        operation = search
    elif path == ORDER_DETAIL_PATH:
        operation = detail
    else:
        return False
    try:
        send_json(operation(data), 200)
    except OrderQueryError as exc:
        send_json(
            {
                "detail": exc.detail,
                "code": exc.code,
                "retryable": exc.retryable,
            },
            exc.status,
        )
    except WafChallengeRequired as exc:
        send_json(
            {
                "detail": str(exc)[:240],
                "code": "waf_verification_required",
                "retryable": True,
            },
            409,
        )
    except RuntimeError as exc:
        send_json(
            {
                "detail": str(exc)[:240] or "订单查询失败",
                "code": "order_query_failed",
                "retryable": True,
            },
            502,
        )
    return True
