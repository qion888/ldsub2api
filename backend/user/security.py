"""Small, dependency-free primitives used by the local user service.

Passwords are never stored in plain text.  Session tokens are intentionally
opaque: only a SHA-256 digest is persisted, so a database copy cannot be
used as a bearer token without first recovering the original token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from typing import Any


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 240_000
PASSWORD_SALT_BYTES = 16
PASSWORD_DIGEST_BYTES = 32


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    """Return a self-describing PBKDF2 password hash."""
    if not isinstance(password, str):
        raise ValueError("password must be a string")
    salt = secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        int(iterations),
        dklen=PASSWORD_DIGEST_BYTES,
    )
    return f"{PASSWORD_SCHEME}${int(iterations)}${_b64(salt)}${_b64(digest)}"


def verify_password(password: Any, encoded: Any) -> bool:
    """Verify a password without raising for malformed persisted values."""
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        iterations = int(raw_iterations)
        if scheme != PASSWORD_SCHEME or not 10_000 <= iterations <= 2_000_000:
            return False
        salt = _unb64(raw_salt)
        expected = _unb64(raw_digest)
        if not salt or not expected:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, UnicodeError, binascii.Error):
        return False


def new_session_token() -> str:
    """Create a URL-safe token suitable for an Authorization header."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()
