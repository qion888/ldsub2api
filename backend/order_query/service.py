"""Order-query workflow with automatic OCR and manual fallback."""

from __future__ import annotations

import base64
import math
import re
import threading
from typing import Any, Callable
from urllib.parse import urlparse

from monitor_core.storefront import WafChallengeRequired

from .captcha import CaptchaRecognizer, normalize_captcha_code
from .client import BASE_URL, CaptchaChallenge, OrderQueryClient
from .complaint import (
    decode_complaint_upload,
    normalize_complaint_history,
    validate_complaint_payload,
)
from .errors import (
    CaptchaRecognizerUnavailable,
    CaptchaVerificationExpired,
    OrderQueryBusy,
    OrderQueryDetailNotFound,
    OrderQueryDetailUnavailable,
    OrderQueryInputError,
    OrderQueryPasswordInvalid,
    OrderQueryPasswordRateLimited,
    OrderQueryPasswordRequired,
    OrderQuerySessionExpired,
    OrderQueryWafVerificationRequired,
    OrderComplaintInputError,
    OrderComplaintSubmissionConflict,
    OrderComplaintSubmissionUnknown,
    OrderComplaintUnavailable,
    UpstreamOrderError,
)
from .sessions import OrderQuerySession, OrderQuerySessionStore

ALLOWED_STATUSES = {999, 0, 1, 2, 3}
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
TRADE_NO_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,159}")


class OrderQueryService:
    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] = OrderQueryClient,
        recognizer: Any = None,
        sessions: OrderQuerySessionStore | None = None,
        max_ocr_attempts: int = 5,
        max_concurrent: int = 2,
    ) -> None:
        self.client_factory = client_factory
        self.recognizer = recognizer or CaptchaRecognizer()
        self.sessions = sessions or OrderQuerySessionStore()
        self.max_ocr_attempts = max(1, min(5, int(max_ocr_attempts)))
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))
        self._complaint_state_lock = threading.Lock()
        self._complaint_submitted_trades: set[str] = set()
        self._complaint_inflight_trades: set[str] = set()
        self._complaint_unknown_trades: set[str] = set()

    @staticmethod
    def _validated_request(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise OrderQueryInputError("订单查询参数必须是 JSON 对象")
        keywords = str(data.get("keywords") or "").strip()
        if not keywords or len(keywords) > 160:
            raise OrderQueryInputError("请输入 1 到 160 个字符的联系方式或订单号")
        try:
            status = int(data.get("status", 999))
            page = int(data.get("page", 1))
            page_size = int(data.get("page_size", 10))
        except (TypeError, ValueError) as exc:
            raise OrderQueryInputError("订单状态或分页参数无效") from exc
        if status not in ALLOWED_STATUSES:
            raise OrderQueryInputError("订单状态筛选无效")
        if page < 1 or page > 1000:
            raise OrderQueryInputError("订单页码应为 1 到 1000")
        if page_size < 1 or page_size > 50:
            raise OrderQueryInputError("每页订单数应为 1 到 50")
        session_id = str(data.get("session_id") or "").strip()
        if session_id and not SESSION_ID_PATTERN.fullmatch(session_id):
            raise OrderQueryInputError("订单查询会话编号无效")
        captcha_code = str(data.get("captcha_code") or "").strip()
        if captcha_code and not normalize_captcha_code(captcha_code):
            raise OrderQueryInputError("验证码格式无效")
        refresh_captcha = data.get("refresh_captcha", False)
        if not isinstance(refresh_captcha, bool):
            raise OrderQueryInputError("refresh_captcha 必须是布尔值")
        return {
            "keywords": keywords,
            "status": status,
            "page": page,
            "page_size": page_size,
            "session_id": session_id,
            "captcha_code": normalize_captcha_code(captcha_code),
            "refresh_captcha": refresh_captcha,
        }

    def _session(self, request: dict[str, Any]) -> OrderQuerySession:
        if request["session_id"]:
            return self.sessions.get(request["session_id"], request["keywords"])
        if request["captcha_code"]:
            raise OrderQueryInputError("手工验证码缺少有效查询会话")
        return self.sessions.create(request["keywords"], self.client_factory())

    @staticmethod
    def _validated_detail_request(data: Any) -> dict[str, str]:
        if not isinstance(data, dict):
            raise OrderQueryInputError("订单详情参数必须是 JSON 对象")
        keywords = str(data.get("keywords") or "").strip()
        if not keywords or len(keywords) > 160:
            raise OrderQueryInputError("订单详情缺少有效的查询内容")
        session_id = str(data.get("session_id") or "").strip()
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise OrderQueryInputError("订单详情缺少有效的查询会话")
        trade_no = str(data.get("trade_no") or "").strip()
        if not TRADE_NO_PATTERN.fullmatch(trade_no):
            raise OrderQueryInputError("订单号格式无效")
        password_value = data.get("query_password", "")
        if not isinstance(password_value, str):
            raise OrderQueryInputError("订单安全密码必须是字符串")
        if len(password_value) > 160 or any(char in password_value for char in "\x00\r\n"):
            raise OrderQueryInputError("订单安全密码格式无效")
        return {
            "keywords": keywords,
            "session_id": session_id,
            "trade_no": trade_no,
            "query_password": password_value,
        }

    @staticmethod
    def _image_data_url(image: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(image).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _new_challenge(
        self,
        session: OrderQuerySession,
        previous_code: str = "",
    ) -> tuple[CaptchaChallenge, bytes, str]:
        challenge = session.client.start_captcha(previous_code)
        image, mime_type = session.client.download_captcha(challenge)
        session.clear_verification()
        session.challenge = challenge
        session.captcha_image = image
        session.captcha_mime = mime_type
        return challenge, image, mime_type

    def _manual_required(
        self,
        session: OrderQuerySession,
        *,
        attempts: int,
        previous_code: str = "",
    ) -> dict[str, Any]:
        _, image, mime_type = self._new_challenge(session, previous_code)
        return {
            "session_id": session.session_id,
            "expires_in": self.sessions.remaining(session),
            "verification": {
                "status": "manual_required",
                "mode": "manual",
                "attempts": attempts,
            },
            "captcha": {"image_data_url": self._image_data_url(image, mime_type)},
        }

    def _with_waf_context(
        self,
        session: OrderQuerySession,
        request: dict[str, Any],
        operation: Callable[[], Any],
    ) -> Any:
        """Attach the live lookup session to any upstream WAF response."""
        try:
            return operation()
        except WafChallengeRequired as exc:
            raise OrderQueryWafVerificationRequired(
                session_id=session.session_id,
                # The interactive browser flow starts after this response
                # reaches the UI. Reserve its lease now, rather than leaving
                # only the normal short lookup TTL for browser startup.
                expires_in=self.sessions.renew(session, ttl_seconds=900),
                request={"status": request["status"], "page": request["page"], "page_size": request["page_size"]},
                detail=str(exc)[:240] or "链动小铺触发阿里云 WAF 滑块验证，请使用浏览器验证后重试",
            ) from exc

    def _automatic_ticket(
        self,
        session: OrderQuerySession,
        *,
        attempts_used: int,
    ) -> tuple[str, int]:
        previous_code = ""
        while attempts_used < self.max_ocr_attempts:
            challenge, image, _ = self._new_challenge(session, previous_code)
            try:
                recognizer_candidates = getattr(self.recognizer, "recognize_candidates", None)
                raw_values = (
                    recognizer_candidates(image)
                    if callable(recognizer_candidates)
                    else [self.recognizer.recognize(image)]
                )
            except CaptchaRecognizerUnavailable:
                return "", attempts_used
            attempts_used += 1
            values = [raw_values] if isinstance(raw_values, str) else raw_values or []
            codes: list[str] = []
            for value in values:
                code = normalize_captcha_code(value)
                if code and code not in codes:
                    codes.append(code)
            if not codes:
                previous_code = ""
                continue
            previous_code = codes[-1]
            for code in codes:
                ticket = session.client.check_captcha(challenge, code)
                if ticket:
                    session.ticket = ticket
                    session.challenge = None
                    session.captcha_image = b""
                    return ticket, attempts_used
        return "", attempts_used

    def _query_verified(
        self,
        session: OrderQuerySession,
        request: dict[str, Any],
        *,
        mode: str,
        attempts: int,
    ) -> dict[str, Any]:
        cache_key = (request["status"], request["page"], request["page_size"])
        now = self.sessions.now()
        cached = session.cached(cache_key, now)
        if cached is None:
            result = session.client.list_orders(
                keywords=request["keywords"],
                ticket=session.ticket,
                status=request["status"],
                page=request["page"],
                page_size=request["page_size"],
            )
            if not isinstance(result, dict) or not isinstance(result.get("orders"), list):
                raise RuntimeError("订单查询客户端返回格式无效")
            cached = {
                "orders": result["orders"],
                "pagination": result["pagination"],
            }
            if self.sessions.cache_seconds:
                session.store_cache(
                    cache_key,
                    cached,
                    now + self.sessions.cache_seconds,
                )
        session.remember_orders(cached["orders"])
        return {
            "session_id": session.session_id,
            "expires_in": self.sessions.remaining(session),
            "verification": {
                "status": "verified",
                "mode": mode,
                "attempts": attempts,
            },
            **cached,
        }

    def _search(self, data: Any) -> dict[str, Any]:
        request = self._validated_request(data)
        session = self._session(request)
        with session.lock:
            if request["refresh_captcha"]:
                return self._with_waf_context(session, request, lambda: self._manual_required(session, attempts=0))

            attempts = 0
            mode = "session"
            if request["captcha_code"]:
                mode = "manual"
                challenge = session.challenge
                if challenge is None:
                    return self._with_waf_context(session, request, lambda: self._manual_required(session, attempts=0))
                attempts = 1
                ticket = self._with_waf_context(
                    session,
                    request,
                    lambda: session.client.check_captcha(challenge, request["captcha_code"]),
                )
                if not ticket:
                    return self._with_waf_context(
                        session,
                        request,
                        lambda: self._manual_required(
                            session,
                            attempts=attempts,
                            previous_code=request["captcha_code"],
                        ),
                    )
                session.ticket = ticket
                session.challenge = None
                session.captcha_image = b""
            elif not session.ticket:
                mode = "ocr"
                ticket, attempts = self._with_waf_context(
                    session,
                    request,
                    lambda: self._automatic_ticket(session, attempts_used=0),
                )
                if not ticket:
                    return self._with_waf_context(session, request, lambda: self._manual_required(session, attempts=attempts))

            try:
                return self._with_waf_context(
                    session,
                    request,
                    lambda: self._query_verified(
                        session,
                        request,
                        mode=mode,
                        attempts=attempts,
                    ),
                )
            except CaptchaVerificationExpired:
                session.clear_verification()

            mode = "ocr"
            ticket, attempts = self._with_waf_context(
                session,
                request,
                lambda: self._automatic_ticket(session, attempts_used=attempts),
            )
            if not ticket:
                return self._with_waf_context(session, request, lambda: self._manual_required(session, attempts=attempts))
            try:
                return self._with_waf_context(
                    session,
                    request,
                    lambda: self._query_verified(
                        session,
                        request,
                        mode=mode,
                        attempts=attempts,
                    ),
                )
            except CaptchaVerificationExpired:
                session.clear_verification()
                return self._with_waf_context(session, request, lambda: self._manual_required(session, attempts=attempts))

    def search(self, data: Any) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise OrderQueryBusy()
        try:
            return self._search(data)
        finally:
            self._slots.release()

    def _detail(self, data: Any) -> dict[str, Any]:
        request = self._validated_detail_request(data)
        session = self.sessions.get(request["session_id"], request["keywords"])
        with session.lock:
            if not session.ticket:
                raise OrderQuerySessionExpired()
            access = session.authorized_order(request["trade_no"])
            if access is None:
                raise OrderQueryDetailNotFound()
            if access["status"] != 1:
                raise OrderQueryDetailUnavailable()
            if access["need_query_password"] and not request["query_password"].strip():
                raise OrderQueryPasswordRequired()
            now = self.sessions.now()
            if not session.password_attempt_allowed(request["trade_no"], now):
                raise OrderQueryPasswordRateLimited()
            try:
                detail = session.client.get_order_detail(
                    trade_no=request["trade_no"],
                    query_password=request["query_password"],
                )
            except OrderQueryPasswordInvalid as exc:
                if session.record_password_failure(request["trade_no"], now):
                    raise OrderQueryPasswordRateLimited() from exc
                raise
            except OrderQuerySessionExpired:
                session.clear_verification()
                raise
            session.clear_password_failure(request["trade_no"])
            return {
                "session_id": session.session_id,
                "expires_in": self.sessions.remaining(session),
                "detail": detail,
            }

    def detail(self, data: Any) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise OrderQueryBusy()
        try:
            return self._detail(data)
        finally:
            self._slots.release()

    @staticmethod
    def _validated_complaint_identity(data: Any) -> dict[str, str]:
        if not isinstance(data, dict):
            raise OrderComplaintInputError("售后申请参数必须是 JSON 对象")
        if any(not isinstance(data.get(field), str) for field in ("keywords", "session_id", "trade_no")):
            raise OrderComplaintInputError("售后申请身份参数格式无效")
        keywords = data["keywords"].strip()
        session_id = data["session_id"].strip()
        trade_no = data["trade_no"].strip()
        if not keywords or len(keywords) > 160:
            raise OrderComplaintInputError("售后申请缺少有效的查询内容")
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise OrderComplaintInputError("售后申请缺少有效的查询会话")
        if not TRADE_NO_PATTERN.fullmatch(trade_no):
            raise OrderComplaintInputError("订单号格式无效")
        return {"keywords": keywords, "session_id": session_id, "trade_no": trade_no}

    def _complaint_session(self, data: Any) -> tuple[dict[str, str], OrderQuerySession]:
        identity = self._validated_complaint_identity(data)
        session = self.sessions.get(identity["session_id"], identity["keywords"])
        if not session.ticket:
            raise OrderQuerySessionExpired()
        if session.authorized_order(identity["trade_no"]) is None:
            raise OrderQueryDetailNotFound()
        return identity, session

    @staticmethod
    def _validated_complaint_history_request(data: Any) -> dict[str, str]:
        """Validate the history endpoint's identity and optional password."""
        if not isinstance(data, dict):
            raise OrderComplaintInputError("售后历史参数必须是 JSON 对象")
        allowed = {"keywords", "session_id", "trade_no", "query_password"}
        unknown = sorted(str(field) for field in data if field not in allowed)
        if unknown:
            raise OrderComplaintInputError("售后历史包含未知字段")
        identity = OrderQueryService._validated_complaint_identity(data)
        password = data.get("query_password", "")
        if not isinstance(password, str):
            raise OrderComplaintInputError("订单安全密码必须是字符串")
        if len(password) > 160 or any(char in password for char in "\x00\r\n"):
            raise OrderComplaintInputError("订单安全密码格式无效")
        return {**identity, "query_password": password}

    @staticmethod
    def _context_flag(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_numeric_code(value: Any) -> bool:
        if type(value) not in (int, float) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, TypeError, ValueError):
            return False

    @staticmethod
    def _strict_success_code(value: Any) -> bool:
        return OrderQueryService._is_numeric_code(value) and value != 0

    @staticmethod
    def _context_status_known(context: dict[str, Any]) -> bool:
        """Return whether the upstream complaint status was explicitly resolved."""
        value = getattr(context, "status_known", context.get("complaint_status_known", False))
        return value is True or (
            type(value) in (int, float)
            and not isinstance(value, bool)
            and value == 1
        )

    @staticmethod
    def _complaint_context_response(
        identity: dict[str, str], session: OrderQuerySession, context: dict[str, Any], remaining: int
    ) -> dict[str, Any]:
        try:
            complaint_status = int(context.get("complaint_status", -1))
        except (TypeError, ValueError):
            complaint_status = -1
        return {
            "session_id": session.session_id,
            "expires_in": remaining,
            "trade_no": identity["trade_no"],
            "can_complaint": OrderQueryService._context_flag(context.get("can_complaint")),
            "complaint_status": complaint_status,
            "complaint_status_known": OrderQueryService._context_status_known(context),
        }

    def complaint_context(self, data: Any) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise OrderQueryBusy()
        try:
            identity, session = self._complaint_session(data)
            with session.lock:
                context = session.client.get_complaint_context(trade_no=identity["trade_no"])
                if not isinstance(context, dict):
                    raise UpstreamOrderError("售后上下文响应格式无效", code="invalid_complaint_context_response")
                session.remember_complaint_context(identity["trade_no"], context)
                if self._context_status_known(context):
                    with self._complaint_state_lock:
                        # Only an explicit upstream status resolves an uncertain submit.
                        self._complaint_unknown_trades.discard(identity["trade_no"])
                    session.clear_complaint_unknown(identity["trade_no"])
                return self._complaint_context_response(
                    identity, session, context, self.sessions.remaining(session)
                )
        finally:
            self._slots.release()

    @staticmethod
    def _history_need_password(value: Any) -> bool:
        """Match the official JavaScript ``need_pwd === 1`` check."""
        return (
            type(value) in (int, float)
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and value == 1
        )

    @staticmethod
    def _history_check_value(value: Any) -> Any:
        if not isinstance(value, dict):
            raise UpstreamOrderError(
                "投诉查询密码状态响应格式无效",
                code="invalid_complaint_history_password_response",
            )
        # Accommodate a raw official response in test/integration clients while
        # keeping the value exposed to the frontend strictly normalized.
        if "data" in value:
            code = value.get("code")
            if code is not None and (
                type(code) not in (int, float)
                or isinstance(code, bool)
                or not math.isfinite(float(code))
                or code != 1
            ):
                raise UpstreamOrderError(
                    "投诉查询密码状态响应格式无效",
                    code="invalid_complaint_history_password_response",
                )
            if not isinstance(value.get("data"), dict):
                raise UpstreamOrderError(
                    "投诉查询密码状态响应格式无效",
                    code="invalid_complaint_history_password_response",
                )
            value = value["data"]
        if "need_pwd" not in value:
            raise UpstreamOrderError(
                "投诉查询密码状态响应格式无效",
                code="invalid_complaint_history_password_response",
            )
        need_pwd = value["need_pwd"]
        if (
            type(need_pwd) not in (int, float)
            or isinstance(need_pwd, bool)
            or not math.isfinite(float(need_pwd))
            or need_pwd not in (0, 1)
        ):
            raise UpstreamOrderError(
                "投诉查询密码状态响应格式无效",
                code="invalid_complaint_history_password_response",
            )
        return int(need_pwd)

    def _complaint_history(self, data: Any) -> dict[str, Any]:
        request = self._validated_complaint_history_request(data)
        identity, session = self._complaint_session(request)
        with session.lock:
            try:
                check_result = session.client.check_need_complaint_password(
                    trade_no=identity["trade_no"],
                )
            except OrderQuerySessionExpired:
                session.clear_verification()
                raise
            need_password = self._history_need_password(self._history_check_value(check_result))
            password = request["query_password"]
            if need_password and not password.strip():
                raise OrderQueryPasswordRequired()

            now = self.sessions.now()
            # Apply the existing cooldown before the upstream call regardless
            # of the latest ``need_pwd`` flag.  This keeps an inconsistent
            # metadata response from bypassing a lock already earned by a
            # password rejection.
            if not session.complaint_password_attempt_allowed(identity["trade_no"], now):
                raise OrderQueryPasswordRateLimited()
            try:
                history_result = session.client.get_complaint_history(
                    trade_no=identity["trade_no"],
                    query_password=password,
                )
            except OrderQueryPasswordInvalid as exc:
                # Count an explicit upstream password rejection even when the
                # preceding metadata said no password was needed.  A stale or
                # inconsistent ``need_pwd`` response must not provide a way
                # around the same order-level backoff used by detail lookup.
                if session.record_complaint_password_failure(identity["trade_no"], now):
                    raise OrderQueryPasswordRateLimited() from exc
                raise
            except OrderQuerySessionExpired:
                session.clear_verification()
                raise

            try:
                complaint = normalize_complaint_history(
                    history_result,
                    expected_trade_no=identity["trade_no"],
                )
            except ValueError as exc:
                raise UpstreamOrderError(
                    "投诉历史响应格式无效",
                    code="invalid_complaint_history_response",
                ) from exc
            session.clear_complaint_password_failure(identity["trade_no"])
            return {
                "session_id": session.session_id,
                "expires_in": self.sessions.remaining(session),
                "trade_no": identity["trade_no"],
                "need_query_password": need_password,
                "complaint": complaint,
            }

    def complaint_history(self, data: Any) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise OrderQueryBusy()
        try:
            return self._complaint_history(data)
        finally:
            self._slots.release()

    @staticmethod
    def _context_or_fetch(
        session: OrderQuerySession,
        trade_no: str,
    ) -> dict[str, Any]:
        context = session.complaint_context(trade_no)
        if context is None:
            context = session.client.get_complaint_context(trade_no=trade_no)
            if not isinstance(context, dict):
                raise UpstreamOrderError("售后上下文响应格式无效", code="invalid_complaint_context_response")
            session.remember_complaint_context(trade_no, context)
        return context

    def complaint_upload(self, data: Any) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise OrderQueryBusy()
        try:
            identity, session = self._complaint_session(data)
            # Decode before taking the session lock so malformed large requests do not block queries.
            if isinstance(data, dict):
                unknown = set(data) - {
                    "keywords", "session_id", "trade_no", "name", "filename",
                    "mime_type", "mime", "type", "data_base64", "base64", "data", "data_url",
                }
                if unknown:
                    raise OrderComplaintInputError("图片上传包含未知字段")
            name, mime_type, content = decode_complaint_upload(data)
            with session.lock:
                context = self._context_or_fetch(session, identity["trade_no"])
                if not self._context_status_known(context):
                    raise UpstreamOrderError(
                        "官方售后状态无法确认，请刷新后重试",
                        code="complaint_context_unknown",
                        status=409,
                        retryable=True,
                    )
                if not self._context_flag(context.get("can_complaint")):
                    raise OrderComplaintUnavailable()
                try:
                    context_status = int(context.get("complaint_status", -1))
                except (TypeError, ValueError):
                    context_status = -1
                if context_status != -1:
                    raise OrderComplaintSubmissionConflict()
                if session.complaint_upload_count(identity["trade_no"]) >= 12:
                    raise OrderComplaintInputError("每个订单最多登记 12 个上传文件")
                uploaded = session.client.upload_complaint_file(
                    content=content,
                    filename=name,
                    mime_type=mime_type,
                )
                if isinstance(uploaded, dict) and "code" in uploaded and not self._strict_success_code(uploaded.get("code")):
                    raise UpstreamOrderError(str(uploaded.get("msg") or "图片上传失败"), code="complaint_upload_failed", retryable=True)
                url = uploaded.get("url") if isinstance(uploaded, dict) else uploaded
                if not isinstance(url, str) or not url:
                    raise UpstreamOrderError("图片上传响应格式无效", code="invalid_complaint_upload_response")
                if url.startswith("/") and not url.startswith("//") and ".." not in url.split("/"):
                    url = f"{BASE_URL}{url}"
                try:
                    parsed_url = urlparse(url)
                    parsed_url.port
                except (UnicodeError, ValueError) as exc:
                    raise UpstreamOrderError("图片上传地址无效", code="invalid_complaint_upload_response") from exc
                if (
                    parsed_url.scheme != "https"
                    or not parsed_url.hostname
                    or parsed_url.username is not None
                    or parsed_url.password is not None
                    or parsed_url.fragment
                ):
                    raise UpstreamOrderError("图片上传地址必须使用 HTTPS", code="invalid_complaint_upload_response")
                if not session.register_complaint_upload(
                    identity["trade_no"],
                    url,
                    name=name,
                    mime_type=mime_type,
                    size=len(content),
                ):
                    raise OrderComplaintInputError("每个订单最多登记 12 个上传文件")
                return {"url": url, "name": name, "mime_type": mime_type, "size": len(content)}
        finally:
            self._slots.release()

    def complaint_remove_upload(self, data: Any) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise OrderQueryBusy()
        try:
            identity, session = self._complaint_session(data)
            url = str(data.get("url") or "").strip() if isinstance(data, dict) else ""
            if not url:
                raise OrderComplaintInputError("缺少要移除的图片地址")
            with session.lock:
                removed = session.remove_complaint_upload(identity["trade_no"], url)
                if not removed:
                    raise OrderComplaintInputError("图片地址未登记")
                return {"removed": True, "url": url}
        finally:
            self._slots.release()

    def complaint_submit(self, data: Any) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise OrderQueryBusy()
        try:
            identity, session = self._complaint_session(data)
            with session.lock:
                with self._complaint_state_lock:
                    if identity["trade_no"] in self._complaint_unknown_trades or session.complaint_submission_unknown(identity["trade_no"]):
                        raise OrderComplaintSubmissionUnknown()
                    if (
                        identity["trade_no"] in self._complaint_submitted_trades
                        or identity["trade_no"] in self._complaint_inflight_trades
                        or session.complaint_submission(identity["trade_no"]) is not None
                    ):
                        raise OrderComplaintSubmissionConflict()
                # Re-read immediately before reserving the trade number so a
                # complaint created after the dialog opened cannot be duplicated.
                context = session.client.get_complaint_context(trade_no=identity["trade_no"])
                if not isinstance(context, dict):
                    raise UpstreamOrderError(
                        "售后上下文响应格式无效",
                        code="invalid_complaint_context_response",
                    )
                session.remember_complaint_context(identity["trade_no"], context)
                if not self._context_status_known(context):
                    raise UpstreamOrderError(
                        "官方售后状态无法确认，请刷新后重试",
                        code="complaint_context_unknown",
                        status=409,
                        retryable=True,
                    )
                if not self._context_flag(context.get("can_complaint")):
                    raise OrderComplaintUnavailable()
                try:
                    status = int(context.get("complaint_status", -1))
                except (TypeError, ValueError):
                    status = -1
                if status != -1:
                    raise OrderComplaintSubmissionConflict()
                complaint_data = {
                    key: value
                    for key, value in (data.items() if isinstance(data, dict) else [])
                    if key not in {"keywords", "session_id"}
                }
                payload = validate_complaint_payload(complaint_data)
                for url in payload["images"]:
                    if session.complaint_upload(identity["trade_no"], url) is None:
                        raise OrderComplaintInputError("证据图片必须先通过本地上传")
                if payload["collect_image"] and session.complaint_upload(identity["trade_no"], payload["collect_image"]) is None:
                    raise OrderComplaintInputError("退款二维码必须先通过本地上传")
                with self._complaint_state_lock:
                    # Context fetching and payload validation happen outside the
                    # global lock, so re-check before reserving the trade number.
                    # Separate query sessions can authorize the same order.
                    if identity["trade_no"] in self._complaint_unknown_trades:
                        raise OrderComplaintSubmissionUnknown()
                    if (
                        identity["trade_no"] in self._complaint_submitted_trades
                        or identity["trade_no"] in self._complaint_inflight_trades
                    ):
                        raise OrderComplaintSubmissionConflict()
                    if not session.begin_complaint_submission(identity["trade_no"]):
                        raise OrderComplaintSubmissionConflict()
                    self._complaint_inflight_trades.add(identity["trade_no"])
                try:
                    submit_result = session.client.submit_complaint(payload)
                    if not isinstance(submit_result, dict):
                        raise OrderComplaintSubmissionUnknown()
                    code = submit_result.get("code")
                    if not self._is_numeric_code(code):
                        raise OrderComplaintSubmissionUnknown()
                    if not self._strict_success_code(code):
                        raise UpstreamOrderError(str(submit_result.get("msg") or "售后申请提交失败"), code="complaint_submit_failed", retryable=True)
                except UpstreamOrderError as exc:
                    if exc.code != "complaint_submit_failed":
                        session.mark_complaint_unknown(identity["trade_no"])
                        with self._complaint_state_lock:
                            self._complaint_inflight_trades.discard(identity["trade_no"])
                            self._complaint_unknown_trades.add(identity["trade_no"])
                        raise OrderComplaintSubmissionUnknown() from exc
                    session.fail_complaint_submission(identity["trade_no"])
                    with self._complaint_state_lock:
                        self._complaint_inflight_trades.discard(identity["trade_no"])
                    raise
                except Exception:
                    # Any non-domain exception leaves the upstream outcome unknown.
                    session.mark_complaint_unknown(identity["trade_no"])
                    with self._complaint_state_lock:
                        self._complaint_inflight_trades.discard(identity["trade_no"])
                        self._complaint_unknown_trades.add(identity["trade_no"])
                    raise OrderComplaintSubmissionUnknown()
                result = {
                    "submitted": True,
                    "trade_no": identity["trade_no"],
                    "complaint_status": 0,
                    "message": "售后申请提交成功",
                }
                session.finish_complaint_submission(identity["trade_no"], result)
                with self._complaint_state_lock:
                    self._complaint_inflight_trades.discard(identity["trade_no"])
                    self._complaint_submitted_trades.add(identity["trade_no"])
                return result
        finally:
            self._slots.release()
