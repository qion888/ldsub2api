"""Bounded in-memory sessions for order captcha and pagination reuse."""

from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import CaptchaChallenge
from .errors import OrderQueryInputError, OrderQuerySessionExpired

MAX_AUTHORIZED_ORDERS = 500
MAX_PASSWORD_FAILURES = 5
PASSWORD_BACKOFF_SECONDS = 60
MAX_COMPLAINT_UPLOADS = 12


@dataclass
class OrderQuerySession:
    session_id: str
    keyword_fingerprint: str
    client: Any
    expires_at: float
    ticket: str = ""
    challenge: CaptchaChallenge | None = None
    captcha_image: bytes = b""
    captcha_mime: str = "image/png"
    cache: dict[tuple[int, int, int], tuple[float, dict[str, Any]]] = field(default_factory=dict)
    authorized_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    password_failures: dict[str, tuple[int, float]] = field(default_factory=dict)
    complaint_password_failures: dict[str, tuple[int, float]] = field(default_factory=dict)
    complaint_contexts: dict[str, dict[str, Any]] = field(default_factory=dict)
    complaint_uploads: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    complaint_submitting: set[str] = field(default_factory=set)
    complaint_unknown: set[str] = field(default_factory=set)
    complaint_submitted: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def clear_verification(self) -> None:
        self.ticket = ""
        self.challenge = None
        self.captcha_image = b""
        self.cache.clear()
        self.authorized_orders.clear()
        self.password_failures.clear()
        self.complaint_password_failures.clear()

    def remember_orders(self, orders: list[Any]) -> None:
        for order in orders:
            if not isinstance(order, dict):
                continue
            trade_no = str(order.get("trade_no") or "").strip()
            if not trade_no:
                continue
            try:
                status = int(order.get("status", -1))
            except (TypeError, ValueError):
                status = -1
            self.authorized_orders.pop(trade_no, None)
            self.authorized_orders[trade_no] = {
                "status": status,
                "need_query_password": order.get("need_query_password") is True,
                "goods_type": str(order.get("goods_type") or "")[:30],
            }
            while len(self.authorized_orders) > MAX_AUTHORIZED_ORDERS:
                oldest = next(iter(self.authorized_orders))
                self.authorized_orders.pop(oldest)
                self.password_failures.pop(oldest, None)
                self.complaint_password_failures.pop(oldest, None)

    def authorized_order(self, trade_no: str) -> dict[str, Any] | None:
        value = self.authorized_orders.get(trade_no)
        return copy.deepcopy(value) if value is not None else None

    def password_attempt_allowed(self, trade_no: str, now: float) -> bool:
        failure = self.password_failures.get(trade_no)
        if failure is None:
            return True
        _, blocked_until = failure
        if blocked_until > now:
            return False
        if blocked_until:
            self.password_failures.pop(trade_no, None)
        return True

    def record_password_failure(self, trade_no: str, now: float) -> bool:
        count, blocked_until = self.password_failures.get(trade_no, (0, 0.0))
        if blocked_until and blocked_until <= now:
            count = 0
        count += 1
        blocked = count >= MAX_PASSWORD_FAILURES
        self.password_failures[trade_no] = (
            count,
            now + PASSWORD_BACKOFF_SECONDS if blocked else 0.0,
        )
        return blocked

    def clear_password_failure(self, trade_no: str) -> None:
        self.password_failures.pop(trade_no, None)

    def complaint_password_attempt_allowed(self, trade_no: str, now: float) -> bool:
        """Check the independent cooldown for complaint-history passwords."""
        failure = self.complaint_password_failures.get(trade_no)
        if failure is None:
            return True
        _, blocked_until = failure
        if blocked_until > now:
            return False
        if blocked_until:
            self.complaint_password_failures.pop(trade_no, None)
        return True

    def record_complaint_password_failure(self, trade_no: str, now: float) -> bool:
        count, blocked_until = self.complaint_password_failures.get(trade_no, (0, 0.0))
        if blocked_until and blocked_until <= now:
            count = 0
        count += 1
        blocked = count >= MAX_PASSWORD_FAILURES
        self.complaint_password_failures[trade_no] = (
            count,
            now + PASSWORD_BACKOFF_SECONDS if blocked else 0.0,
        )
        return blocked

    def clear_complaint_password_failure(self, trade_no: str) -> None:
        self.complaint_password_failures.pop(trade_no, None)

    def remember_complaint_context(self, trade_no: str, context: dict[str, Any]) -> None:
        self.complaint_contexts[trade_no] = copy.deepcopy(context)

    def complaint_context(self, trade_no: str) -> dict[str, Any] | None:
        value = self.complaint_contexts.get(trade_no)
        return copy.deepcopy(value) if value is not None else None

    def register_complaint_upload(
        self,
        trade_no: str,
        url: str,
        *,
        name: str,
        mime_type: str,
        size: int,
    ) -> bool:
        uploads = self.complaint_uploads.setdefault(trade_no, {})
        if url not in uploads and len(uploads) >= MAX_COMPLAINT_UPLOADS:
            return False
        uploads[url] = {"url": url, "name": name, "mime_type": mime_type, "size": int(size)}
        return True

    def remove_complaint_upload(self, trade_no: str, url: str) -> bool:
        uploads = self.complaint_uploads.get(trade_no)
        if not uploads or url not in uploads:
            return False
        uploads.pop(url, None)
        return True

    def complaint_upload(self, trade_no: str, url: str) -> dict[str, Any] | None:
        value = self.complaint_uploads.get(trade_no, {}).get(url)
        return copy.deepcopy(value) if value is not None else None

    def complaint_upload_count(self, trade_no: str) -> int:
        return len(self.complaint_uploads.get(trade_no, {}))

    def begin_complaint_submission(self, trade_no: str) -> bool:
        if trade_no in self.complaint_submitted or trade_no in self.complaint_submitting or trade_no in self.complaint_unknown:
            return False
        self.complaint_submitting.add(trade_no)
        return True

    def finish_complaint_submission(self, trade_no: str, result: dict[str, Any]) -> None:
        self.complaint_submitting.discard(trade_no)
        self.complaint_submitted[trade_no] = copy.deepcopy(result)

    def fail_complaint_submission(self, trade_no: str) -> None:
        self.complaint_submitting.discard(trade_no)

    def mark_complaint_unknown(self, trade_no: str) -> None:
        self.complaint_submitting.discard(trade_no)
        self.complaint_unknown.add(trade_no)

    def clear_complaint_unknown(self, trade_no: str) -> None:
        self.complaint_unknown.discard(trade_no)

    def complaint_submission_unknown(self, trade_no: str) -> bool:
        return trade_no in self.complaint_unknown

    def complaint_submission(self, trade_no: str) -> dict[str, Any] | None:
        value = self.complaint_submitted.get(trade_no)
        return copy.deepcopy(value) if value is not None else None

    def cached(self, key: tuple[int, int, int], now: float) -> dict[str, Any] | None:
        entry = self.cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            self.cache.pop(key, None)
            return None
        return copy.deepcopy(value)

    def store_cache(self, key: tuple[int, int, int], value: dict[str, Any], expires_at: float) -> None:
        self.cache[key] = (expires_at, copy.deepcopy(value))
        if len(self.cache) > 8:
            oldest = min(self.cache, key=lambda item: self.cache[item][0])
            self.cache.pop(oldest, None)


class OrderQuerySessionStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_sessions: int = 64,
        cache_seconds: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(30, min(600, int(ttl_seconds)))
        self.max_sessions = max(1, int(max_sessions))
        self.cache_seconds = max(0, min(30, int(cache_seconds)))
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._sessions: dict[str, OrderQuerySession] = {}
        self._lock = threading.Lock()

    def _fingerprint(self, keywords: str) -> str:
        return hmac.new(self._secret, keywords.encode("utf-8"), hashlib.sha256).hexdigest()

    def _prune(self, now: float) -> None:
        for session_id in [
            key for key, value in self._sessions.items() if value.expires_at <= now
        ]:
            self._sessions.pop(session_id, None)
        while len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].expires_at)
            self._sessions.pop(oldest, None)

    def create(self, keywords: str, client: Any) -> OrderQuerySession:
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = OrderQuerySession(
                session_id=secrets.token_urlsafe(24),
                keyword_fingerprint=self._fingerprint(keywords),
                client=client,
                expires_at=now + self.ttl_seconds,
            )
            self._sessions[session.session_id] = session
            return session

    def get(self, session_id: str, keywords: str) -> OrderQuerySession:
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(str(session_id or ""))
            if session is None:
                raise OrderQuerySessionExpired()
            if not hmac.compare_digest(session.keyword_fingerprint, self._fingerprint(keywords)):
                raise OrderQueryInputError("订单查询会话与查询内容不匹配")
            return session

    def remaining(self, session: OrderQuerySession) -> int:
        return max(0, int(session.expires_at - self._clock()))

    def renew(self, session: OrderQuerySession, *, ttl_seconds: int | None = None) -> int:
        """Extend a live session for a bounded interactive verification lease."""
        now = self._clock()
        lease = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        lease = max(30, min(1800, lease))
        with self._lock:
            if self._sessions.get(session.session_id) is not session or session.expires_at <= now:
                self._sessions.pop(session.session_id, None)
                raise OrderQuerySessionExpired()
            with session.lock:
                session.expires_at = max(session.expires_at, now + lease)
                return max(0, int(session.expires_at - now))

    def now(self) -> float:
        return self._clock()

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
