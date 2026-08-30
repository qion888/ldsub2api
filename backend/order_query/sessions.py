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
    lock: threading.RLock = field(default_factory=threading.RLock)

    def clear_verification(self) -> None:
        self.ticket = ""
        self.challenge = None
        self.captcha_image = b""
        self.cache.clear()

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

    def now(self) -> float:
        return self._clock()

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
