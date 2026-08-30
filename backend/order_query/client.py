"""Cookie-aware HTTP client for the LinkDong order lookup API."""

from __future__ import annotations

import json
import math
import mimetypes
import re
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from monitor_core.storefront import ALLOWED_HOST, USER_AGENT, WAF_MARKERS, WafChallengeRequired

from .captcha import captcha_sign
from .complaint import normalize_complaint_history
from .detail import normalize_order_detail
from .errors import (
    CaptchaVerificationExpired,
    OrderQueryDetailNotFound,
    OrderQueryPasswordInvalid,
    OrderQuerySessionExpired,
    UpstreamOrderError,
)

BASE_URL = f"https://{ALLOWED_HOST}"
CAPTCHA_START_PATH = "/shopApi/Common/captchaStart"
ORDER_LIST_PATH = "/shopApi/Order/list"
ORDER_DETAIL_PATH = "/shopApi/Order/info"
COMPLAINT_INFO_PATH = "/shopApi/Order/info"
COMPLAINT_PASSWORD_CHECK_PATH = "/shopApi/Order/checkNeedComplaintPwd"
COMPLAINT_HISTORY_PATH = "/shopApi/Order/complaintInfo"
COMPLAINT_UPLOAD_PATH = "/shopApi/upload/file"
COMPLAINT_SUBMIT_PATH = "/shopApi/Order/complaintOrder"
# Public aliases used by route-level tests and integrations.
COMPLAINT_INFO_ENDPOINT = COMPLAINT_INFO_PATH
COMPLAINT_PASSWORD_CHECK_ENDPOINT = COMPLAINT_PASSWORD_CHECK_PATH
COMPLAINT_HISTORY_ENDPOINT = COMPLAINT_HISTORY_PATH
COMPLAINT_CHECK_NEED_PASSWORD_PATH = COMPLAINT_PASSWORD_CHECK_PATH
COMPLAINT_CHECK_NEED_PASSWORD_ENDPOINT = COMPLAINT_PASSWORD_CHECK_PATH
COMPLAINT_INFO_HISTORY_PATH = COMPLAINT_HISTORY_PATH
COMPLAINT_INFO_HISTORY_ENDPOINT = COMPLAINT_HISTORY_PATH
COMPLAINT_UPLOAD_ENDPOINT = COMPLAINT_UPLOAD_PATH
COMPLAINT_SUBMIT_ENDPOINT = COMPLAINT_SUBMIT_PATH
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CAPTCHA_BYTES = 512 * 1024
STATUS_LABELS = {
    0: "待付款",
    1: "已付款",
    2: "已关闭",
    3: "已退款",
}
ALLOWED_CAPTCHA_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}


@dataclass(frozen=True)
class CaptchaChallenge:
    image_url: str
    check_url: str
    ip: str


class ComplaintContext(dict[str, Any]):
    """Normalized context with an internal flag for resolving uncertain submits."""

    def __init__(self, values: dict[str, Any], *, status_known: bool) -> None:
        super().__init__(values)
        self.status_known = bool(status_known)


def _safe_text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _integer(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


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


def _public_image_url(value: Any) -> str:
    text = _safe_text(value, 1000)
    parsed = urlparse(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def normalize_order(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    trade_no = _safe_text(item.get("trade_no"), 160)
    if not trade_no:
        return None
    goods = item.get("goods") if isinstance(item.get("goods"), dict) else {}
    goods_key = _safe_text(goods.get("goods_key") or item.get("goods_key"), 80)
    goods_type = _safe_text(goods.get("goods_type") or item.get("goods_type"), 30)
    status = _integer(item.get("status"), -1)
    complaint = item.get("complaint") if isinstance(item.get("complaint"), dict) else {}
    complaint_status = complaint.get("status")
    try:
        complaint_status = int(complaint_status) if complaint_status is not None else None
    except (TypeError, ValueError):
        complaint_status = None
    encoded_trade = quote(trade_no, safe="")
    encoded_goods = quote(goods_key, safe="")
    return {
        "trade_no": trade_no,
        "goods_name": _safe_text(item.get("goods_name") or goods.get("name"), 200),
        "goods_key": goods_key,
        "goods_type": goods_type,
        "goods_image": _public_image_url(goods.get("image") or goods.get("cover")),
        "created_at": _created_at(item.get("create_time")),
        "total_amount": _money(item.get("total_amount")),
        "quantity": max(0, _integer(item.get("quantity"))),
        "status": status,
        "status_label": STATUS_LABELS.get(status, "未知状态"),
        "need_query_password": _integer(item.get("need_query_password")) == 1,
        "can_complaint": _integer(item.get("can_complaint")) == 1,
        "complaint_status": complaint_status,
        "detail_url": f"{BASE_URL}/order/info/{encoded_trade}",
        "result_url": f"{BASE_URL}/order/result/{encoded_trade}",
        "goods_url": f"{BASE_URL}/item/{encoded_goods}" if goods_key else "",
    }


def normalize_order_list(payload: Any, *, page: int, page_size: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UpstreamOrderError("订单接口响应格式无效", code="invalid_order_response")
    if payload.get("code") != 1:
        message = _safe_text(payload.get("msg") or "订单查询失败")
        if "人机" in message or "验证" in message:
            raise CaptchaVerificationExpired()
        raise UpstreamOrderError(message, code="upstream_order_rejected")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise UpstreamOrderError("订单接口未返回有效列表", code="invalid_order_response")
    raw_orders = data.get("list")
    if raw_orders is None:
        raw_orders = []
    if not isinstance(raw_orders, list):
        raise UpstreamOrderError("订单列表格式无效", code="invalid_order_response")
    orders = [normalized for item in raw_orders if (normalized := normalize_order(item)) is not None]
    total = max(0, _integer(data.get("total"), len(orders)))
    return {
        "orders": orders,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, math.ceil(total / page_size)),
        },
    }


class OrderQueryClient:
    def __init__(self, *, timeout: float = 12.0, opener: Any = None) -> None:
        self.timeout = timeout
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
        self.visitor_id = "".join(secrets.choice(alphabet) for _ in range(9))
        self.cookie_jar = CookieJar()
        self.opener = opener or build_opener(HTTPCookieProcessor(self.cookie_jar))

    @staticmethod
    def _validated_url(url: str, allowed_paths: set[str]) -> str:
        parsed = urlparse(str(url or ""))
        try:
            port = parsed.port
        except ValueError as exc:
            raise UpstreamOrderError("验证码接口地址无效", code="invalid_captcha_url") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != ALLOWED_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.fragment
            or parsed.path.lower() not in {path.lower() for path in allowed_paths}
        ):
            raise UpstreamOrderError("验证码接口地址无效", code="invalid_captcha_url")
        return parsed.geturl()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/order",
            "Visitorid": self.visitor_id,
        }

    def _open(self, request: Request) -> Any:
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            if exc.code == 429:
                raise UpstreamOrderError(
                    "链动小铺订单接口请求过于频繁",
                    code="upstream_rate_limited",
                    status=429,
                    retryable=True,
                ) from exc
            raise UpstreamOrderError(
                f"链动小铺订单接口返回 HTTP {exc.code}",
                code="upstream_http_error",
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise UpstreamOrderError(
                "链动小铺订单接口请求超时",
                code="upstream_timeout",
                status=504,
                retryable=True,
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise UpstreamOrderError(
                    "链动小铺订单接口请求超时",
                    code="upstream_timeout",
                    status=504,
                    retryable=True,
                ) from exc
            raise UpstreamOrderError(
                "无法连接链动小铺订单接口",
                code="upstream_unavailable",
                retryable=True,
            ) from exc

    def _json_request(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with self._open(request) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
        if len(raw) > MAX_JSON_BYTES:
            raise UpstreamOrderError("订单接口响应过大", code="upstream_response_too_large")
        if any(marker in raw for marker in WAF_MARKERS):
            raise WafChallengeRequired("链动小铺触发阿里云 WAF 验证")
        if "text/html" in content_type.lower():
            raise UpstreamOrderError("订单接口返回了 HTML 页面", code="invalid_order_response")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamOrderError("订单接口返回了无法解析的数据", code="invalid_order_response") from exc
        if not isinstance(result, dict):
            raise UpstreamOrderError("订单接口响应格式无效", code="invalid_order_response")
        return result

    def _form_request(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send an official form-encoded request using the current cookie/session context."""
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        encoded = urlencode(payload, doseq=True, encoding="utf-8").encode("utf-8")
        return self._request_json_with_headers(url, encoded, headers=headers)

    @staticmethod
    def _response_json(raw: bytes, content_type: str) -> dict[str, Any]:
        if len(raw) > MAX_JSON_BYTES:
            raise UpstreamOrderError("订单接口响应过大", code="upstream_response_too_large")
        if any(marker in raw for marker in WAF_MARKERS):
            raise WafChallengeRequired("链动小铺触发阿里云 WAF 验证")
        if "text/html" in content_type.lower():
            raise UpstreamOrderError("订单接口返回了 HTML 页面", code="invalid_order_response")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamOrderError("订单接口返回了无法解析的数据", code="invalid_order_response") from exc
        if not isinstance(result, dict):
            raise UpstreamOrderError("订单接口响应格式无效", code="invalid_order_response")
        return result

    def _request_json_with_headers(
        self,
        url: str,
        payload: bytes,
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        request = Request(url, data=payload, headers=headers, method="POST")
        with self._open(request) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
        return self._response_json(raw, content_type)

    @staticmethod
    def _response_message(result: dict[str, Any], fallback: str) -> str:
        value = result.get("msg") or result.get("message") or fallback
        return _safe_text(value, 240)

    @staticmethod
    def _as_flag(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return _integer(value, 0) == 1

    @staticmethod
    def _strict_numeric_code(value: Any, expected: int) -> bool:
        return OrderQueryClient._is_numeric_code(value) and value == expected

    @staticmethod
    def _nonzero_numeric_code(value: Any) -> bool:
        return OrderQueryClient._is_numeric_code(value) and value != 0

    @staticmethod
    def _is_numeric_code(value: Any) -> bool:
        if type(value) not in (int, float) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, TypeError, ValueError):
            return False

    def get_complaint_context(self, trade_no: str) -> dict[str, Any]:
        """Fetch the order and system complaint flags using the query session cookies."""
        info = self._json_request(f"{BASE_URL}{COMPLAINT_INFO_PATH}", {"trade_no": trade_no})
        if not self._strict_numeric_code(info.get("code"), 1) or not isinstance(info.get("data"), dict):
            raise UpstreamOrderError(
                self._response_message(info, "订单信息获取失败"),
                code="complaint_context_failed",
                retryable=True,
            )
        order = info["data"]
        returned_trade_no = _safe_text(order.get("trade_no"), 160)
        if not returned_trade_no or returned_trade_no != _safe_text(trade_no, 160):
            raise UpstreamOrderError(
                "订单信息与请求订单号不一致",
                code="complaint_context_mismatch",
            )
        status_known = False
        if isinstance(order.get("complaint"), dict) and "status" in order["complaint"]:
            status_value = order["complaint"].get("status")
            status_known = True
        elif "complaint_status" in order:
            status_value = order.get("complaint_status")
            status_known = True
        elif "complaint" not in order or order.get("complaint") is None:
            # The official page treats a missing/null complaint as no record.
            status_value = -1
            status_known = True
        else:
            status_value = -1
        try:
            complaint_status = int(status_value)
        except (TypeError, ValueError):
            complaint_status = -1
            status_known = False
        return ComplaintContext(
            {
                "can_complaint": self._as_flag(order.get("can_complaint")),
                "complaint_status": complaint_status,
            },
            status_known=status_known,
        )

    def upload_complaint_file(
        self,
        content: bytes,
        filename: str,
        mime_type: str,
    ) -> str:
        """Upload one complaint image as the official multipart ``file`` field."""
        safe_name = re.sub(r"[\x00\r\n\\/\"]", "_", str(filename or "file"))[:180] or "file"
        mime_type = str(mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream").lower()
        boundary = "----LDXPComplaint" + secrets.token_hex(16)
        preamble = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        body = preamble + bytes(content) + f"\r\n--{boundary}--\r\n".encode("ascii")
        result = self._request_json_with_headers(
            f"{BASE_URL}{COMPLAINT_UPLOAD_PATH}",
            body,
            headers={
                **self._headers(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        if not self._nonzero_numeric_code(result.get("code")):
            raise UpstreamOrderError(
                self._response_message(result, "图片上传失败"),
                code="complaint_upload_failed",
                retryable=True,
            )
        data = result.get("data")
        url = data.get("url") if isinstance(data, dict) else None
        try:
            parsed = urlparse(str(url or "").strip())
            hostname = parsed.hostname
            parsed.port
        except (UnicodeError, ValueError) as exc:
            raise UpstreamOrderError("图片上传接口未返回有效地址", code="invalid_complaint_upload_response") from exc
        relative_path = (
            not parsed.scheme
            and not parsed.netloc
            and parsed.path.startswith("/")
            and ".." not in parsed.path.split("/")
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
        absolute_url = (
            parsed.scheme == "https"
            and bool(hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
        if not (absolute_url or relative_path):
            raise UpstreamOrderError("图片上传接口未返回有效地址", code="invalid_complaint_upload_response")
        if relative_path:
            return f"{BASE_URL}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
        return parsed.geturl()

    def submit_complaint(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit the exact official complaint payload and return its response."""
        result = self._json_request(f"{BASE_URL}{COMPLAINT_SUBMIT_PATH}", payload)
        if not self._is_numeric_code(result.get("code")):
            raise UpstreamOrderError(
                "售后申请接口响应格式无效",
                code="invalid_complaint_submit_response",
            )
        if not self._nonzero_numeric_code(result.get("code")):
            raise UpstreamOrderError(
                self._response_message(result, "售后申请提交失败"),
                code="complaint_submit_failed",
                retryable=True,
            )
        return result

    # Compatibility names for callers that use the endpoint terminology.
    get_complaint_info = get_complaint_context
    upload_file = upload_complaint_file
    submit_complaint_order = submit_complaint

    def start_captcha(self, previous_code: str = "") -> CaptchaChallenge:
        result = self._json_request(f"{BASE_URL}{CAPTCHA_START_PATH}", {"code": previous_code})
        if result.get("code") != 1 or not isinstance(result.get("data"), dict):
            raise UpstreamOrderError(
                _safe_text(result.get("msg") or "无法创建订单验证码"),
                code="captcha_start_failed",
                retryable=True,
            )
        data = result["data"]
        image_url = self._validated_url(
            _safe_text(data.get("img_url"), 1000),
            {"/shopApi/common/captchaImg.html"},
        )
        check_url = self._validated_url(
            _safe_text(data.get("check_url"), 1000),
            {"/shopApi/common/captchaCheck.html"},
        )
        ip = _safe_text(data.get("ip"), 128)
        if not ip:
            raise UpstreamOrderError("验证码接口未返回签名参数", code="invalid_captcha_response")
        return CaptchaChallenge(image_url=image_url, check_url=check_url, ip=ip)

    def download_captcha(self, challenge: CaptchaChallenge) -> tuple[bytes, str]:
        image_url = self._validated_url(challenge.image_url, {"/shopApi/common/captchaImg.html"})
        request = Request(
            image_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
                "Referer": f"{BASE_URL}/order",
                "Visitorid": self.visitor_id,
            },
            method="GET",
        )
        with self._open(request) as response:
            raw = response.read(MAX_CAPTCHA_BYTES + 1)
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
        if len(raw) > MAX_CAPTCHA_BYTES:
            raise UpstreamOrderError("验证码图片过大", code="captcha_image_too_large")
        if any(marker in raw for marker in WAF_MARKERS):
            raise WafChallengeRequired("链动小铺触发阿里云 WAF 验证")
        mime_type = content_type.partition(";")[0].strip().lower()
        if not raw or mime_type not in ALLOWED_CAPTCHA_MIME_TYPES:
            raise UpstreamOrderError("验证码图片响应无效", code="invalid_captcha_image")
        return raw, mime_type

    def check_captcha(self, challenge: CaptchaChallenge, code: str) -> str | None:
        check_url = self._validated_url(challenge.check_url, {"/shopApi/common/captchaCheck.html"})
        result = self._json_request(
            check_url,
            {"code": code, "sign": captcha_sign(code, challenge.ip)},
        )
        if result.get("code") != 1 or not isinstance(result.get("data"), dict):
            return None
        ticket = _safe_text(result["data"].get("ticket"), 512)
        return ticket or None

    def list_orders(
        self,
        *,
        keywords: str,
        ticket: str,
        status: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        result = self._json_request(
            f"{BASE_URL}{ORDER_LIST_PATH}",
            {
                "status": status,
                "current": page,
                "pageSize": page_size,
                "keywords": keywords,
                "ticket": ticket,
            },
        )
        return normalize_order_list(result, page=page, page_size=page_size)

    def get_order_detail(self, *, trade_no: str, query_password: str) -> dict[str, Any]:
        result = self._json_request(
            f"{BASE_URL}{ORDER_DETAIL_PATH}",
            {
                "trade_no": trade_no,
                "query_password": query_password,
                "dump": 1,
            },
        )
        return normalize_order_detail(result, expected_trade_no=trade_no)

    def check_need_complaint_password(self, *, trade_no: str) -> dict[str, Any]:
        """Read whether the official complaint history requires a password."""
        result = self._form_request(
            f"{BASE_URL}{COMPLAINT_PASSWORD_CHECK_PATH}",
            {"trade_no": trade_no},
        )
        if not self._strict_numeric_code(result.get("code"), 1) or not isinstance(result.get("data"), dict):
            self._complaint_history_error(
                result,
                fallback="投诉查询密码状态获取失败",
                code="complaint_history_password_check_failed",
            )
        data = result["data"]
        returned_trade_no = _safe_text(data.get("trade_no"), 160)
        if returned_trade_no and returned_trade_no != _safe_text(trade_no, 160):
            raise UpstreamOrderError(
                "投诉历史订单号与请求不一致",
                code="complaint_history_context_mismatch",
            )
        # The official Vue code uses ``need_pwd === 1``. Reject missing,
        # boolean, string, and out-of-range values rather than failing open.
        need_pwd = data.get("need_pwd")
        if not self._is_numeric_code(need_pwd) or need_pwd not in (0, 1):
            raise UpstreamOrderError(
                "投诉查询密码状态响应格式无效",
                code="invalid_complaint_history_password_response",
            )
        return {"need_pwd": int(need_pwd)}

    @staticmethod
    def _complaint_history_error(
        result: dict[str, Any],
        *,
        fallback: str = "投诉历史获取失败",
        code: str = "complaint_history_failed",
    ) -> None:
        message = _safe_text(result.get("msg") or result.get("message"), 240)
        if any(token in message for token in ("安全密码", "查询密码", "密码错误", "密码不正确")):
            raise OrderQueryPasswordInvalid()
        message_lower = message.lower()
        if (
            any(token in message for token in ("重新查询", "会话已过期", "验证码已过期", "验证已过期", "人机验证", "人机校验"))
            or "human verification" in message_lower
            or ("captcha" in message_lower and any(token in message_lower for token in ("fail", "invalid", "expired")))
        ):
            raise OrderQuerySessionExpired()
        if any(token in message for token in ("订单不存在", "未找到订单", "订单号不存在")):
            raise OrderQueryDetailNotFound()
        if any(token in message for token in ("接口不存在", "接口未找到", "路径不存在")):
            raise UpstreamOrderError(
                "官方售后记录接口暂不可用，请重启后端服务或稍后重试",
                code="complaint_history_endpoint_unavailable",
                status=503,
                retryable=True,
            )
        raise UpstreamOrderError(
            message or fallback,
            code=code,
            retryable=True,
        )

    def get_complaint_history(
        self,
        *,
        trade_no: str,
        query_password: str = "",
    ) -> dict[str, Any]:
        """Fetch and normalize the official complaint history (read-only)."""
        result = self._form_request(
            f"{BASE_URL}{COMPLAINT_HISTORY_PATH}",
            {"trade_no": trade_no, "query_pwd": query_password},
        )
        if not self._strict_numeric_code(result.get("code"), 1):
            self._complaint_history_error(result)
        try:
            return normalize_complaint_history(
                result.get("data"),
                expected_trade_no=trade_no,
            )
        except ValueError as exc:
            raise UpstreamOrderError(
                "投诉历史响应格式无效",
                code="invalid_complaint_history_response",
            ) from exc

    # Compatibility names mirror the endpoint names used in the official JS.
    checkNeedComplaintPwd = check_need_complaint_password
    check_complaint_password = check_need_complaint_password
    complaintInfo = get_complaint_history
    complaint_info = get_complaint_history
    get_complaint_info_history = get_complaint_history
