"""Order-query workflow with automatic OCR and manual fallback."""

from __future__ import annotations

import base64
import re
import threading
from typing import Any, Callable

from .captcha import CaptchaRecognizer, normalize_captcha_code
from .client import CaptchaChallenge, OrderQueryClient
from .errors import (
    CaptchaRecognizerUnavailable,
    CaptchaVerificationExpired,
    OrderQueryBusy,
    OrderQueryInputError,
)
from .sessions import OrderQuerySession, OrderQuerySessionStore

ALLOWED_STATUSES = {999, 0, 1, 2, 3}
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")


class OrderQueryService:
    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] = OrderQueryClient,
        recognizer: Any = None,
        sessions: OrderQuerySessionStore | None = None,
        max_ocr_attempts: int = 3,
        max_concurrent: int = 2,
    ) -> None:
        self.client_factory = client_factory
        self.recognizer = recognizer or CaptchaRecognizer()
        self.sessions = sessions or OrderQuerySessionStore()
        self.max_ocr_attempts = max(1, min(3, int(max_ocr_attempts)))
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))

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
        session.challenge = challenge
        session.captcha_image = image
        session.captcha_mime = mime_type
        session.ticket = ""
        session.cache.clear()
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
                code = normalize_captcha_code(self.recognizer.recognize(image))
            except CaptchaRecognizerUnavailable:
                return "", attempts_used
            attempts_used += 1
            if not code:
                previous_code = ""
                continue
            previous_code = code
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
                return self._manual_required(session, attempts=0)

            attempts = 0
            mode = "session"
            if request["captcha_code"]:
                mode = "manual"
                challenge = session.challenge
                if challenge is None:
                    return self._manual_required(session, attempts=0)
                attempts = 1
                ticket = session.client.check_captcha(challenge, request["captcha_code"])
                if not ticket:
                    return self._manual_required(
                        session,
                        attempts=attempts,
                        previous_code=request["captcha_code"],
                    )
                session.ticket = ticket
                session.challenge = None
                session.captcha_image = b""
            elif not session.ticket:
                mode = "ocr"
                ticket, attempts = self._automatic_ticket(session, attempts_used=0)
                if not ticket:
                    return self._manual_required(session, attempts=attempts)

            try:
                return self._query_verified(
                    session,
                    request,
                    mode=mode,
                    attempts=attempts,
                )
            except CaptchaVerificationExpired:
                session.clear_verification()

            mode = "ocr"
            ticket, attempts = self._automatic_ticket(session, attempts_used=attempts)
            if not ticket:
                return self._manual_required(session, attempts=attempts)
            try:
                return self._query_verified(
                    session,
                    request,
                    mode=mode,
                    attempts=attempts,
                )
            except CaptchaVerificationExpired:
                session.clear_verification()
                return self._manual_required(session, attempts=attempts)

    def search(self, data: Any) -> dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise OrderQueryBusy()
        try:
            return self._search(data)
        finally:
            self._slots.release()
