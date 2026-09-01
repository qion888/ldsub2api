"""HTTP route adapter for order lookup."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

from monitor_core.storefront import WafChallengeRequired

from .complaint import build_complaint_preview
from .errors import OrderQueryError

SendJson = Callable[[Any, int], Any]
ORDER_QUERY_PATH = "/api/order-query/search"
ORDER_DETAIL_PATH = "/api/order-query/detail"
ORDER_WAF_START_PATH = "/api/order-query/waf-verification/start"
ORDER_WAF_COMPLETE_PATH = "/api/order-query/waf-verification/complete"
ORDER_WAF_STATUS_PATH = "/api/order-query/waf-verification/status"
ORDER_COMPLAINT_PREVIEW_PATH = "/api/order-query/complaints/preview"
ORDER_COMPLAINT_CONTEXT_PATH = "/api/order-query/complaints/context"
ORDER_COMPLAINT_HISTORY_PATH = "/api/order-query/complaints/history"
ORDER_COMPLAINT_UPLOAD_PATH = "/api/order-query/complaints/upload"
ORDER_COMPLAINT_SUBMIT_PATH = "/api/order-query/complaints/submit"
ORDER_COMPLAINT_REMOVE_UPLOAD_PATH = "/api/order-query/complaints/upload/remove"
ORDER_QUERY_POST_PATHS = frozenset({
    ORDER_QUERY_PATH,
    ORDER_DETAIL_PATH,
    ORDER_WAF_START_PATH,
    ORDER_WAF_COMPLETE_PATH,
    ORDER_WAF_STATUS_PATH,
    ORDER_COMPLAINT_PREVIEW_PATH,
    ORDER_COMPLAINT_CONTEXT_PATH,
    ORDER_COMPLAINT_HISTORY_PATH,
    ORDER_COMPLAINT_UPLOAD_PATH,
    ORDER_COMPLAINT_SUBMIT_PATH,
    ORDER_COMPLAINT_REMOVE_UPLOAD_PATH,
})
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
    if path not in ORDER_QUERY_POST_PATHS:
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
    complaint_preview: Callable[[dict[str, Any]], dict[str, Any]] = build_complaint_preview,
    complaint_context: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    complaint_history: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    complaint_upload: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    complaint_submit: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    complaint_remove_upload: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> bool:
    if path == ORDER_QUERY_PATH:
        operation = search
    elif path == ORDER_DETAIL_PATH:
        operation = detail
    elif path == ORDER_COMPLAINT_PREVIEW_PATH:
        operation = complaint_preview
    elif path == ORDER_COMPLAINT_CONTEXT_PATH:
        operation = complaint_context
    elif path == ORDER_COMPLAINT_HISTORY_PATH:
        operation = complaint_history
    elif path == ORDER_COMPLAINT_UPLOAD_PATH:
        operation = complaint_upload
    elif path == ORDER_COMPLAINT_SUBMIT_PATH:
        operation = complaint_submit
    elif path == ORDER_COMPLAINT_REMOVE_UPLOAD_PATH:
        operation = complaint_remove_upload
    else:
        return False
    if operation is None:
        if path == ORDER_COMPLAINT_HISTORY_PATH:
            send_json(
                {
                    "detail": "售后记录接口未启用，请重启后端服务",
                    "code": "order_complaint_history_unavailable",
                    "retryable": True,
                },
                503,
            )
            return True
        return False
    try:
        send_json(operation(data), 200)
    except OrderQueryError as exc:
        payload = {
            "detail": exc.detail,
            "code": exc.code,
            "retryable": exc.retryable,
        }
        if hasattr(exc, "session_id"):
            payload["session_id"] = str(getattr(exc, "session_id"))
            payload["expires_in"] = int(getattr(exc, "expires_in", 0))
            payload["waf_request"] = dict(getattr(exc, "request", {}))
        if hasattr(exc, "seconds"):
            payload["cooldown_seconds"] = int(getattr(exc, "seconds"))
        send_json(payload, exc.status)
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
        is_complaint = path.startswith("/api/order-query/complaints/")
        is_complaint_preview = path == ORDER_COMPLAINT_PREVIEW_PATH
        is_complaint_history = path == ORDER_COMPLAINT_HISTORY_PATH
        fallback_detail = (
            "售后历史获取失败"
            if is_complaint_history
            else ("售后申请参数预览失败" if is_complaint_preview else ("售后申请失败" if is_complaint else "订单查询失败"))
        )
        fallback_code = (
            "order_complaint_history_failed"
            if is_complaint_history
            else ("order_complaint_preview_failed" if is_complaint_preview else ("order_complaint_failed" if is_complaint else "order_query_failed"))
        )
        send_json(
            {
                "detail": str(exc)[:240] or fallback_detail,
                "code": fallback_code,
                "retryable": True,
            },
            502,
        )
    return True
