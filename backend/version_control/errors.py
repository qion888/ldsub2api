"""Typed errors returned by the version-control HTTP adapter."""

from __future__ import annotations

from typing import Any


class VersionControlError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        code: str = "version_control_error",
        status: int = 500,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status = status
        self.retryable = retryable

    def payload(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "code": self.code,
            "retryable": self.retryable,
        }
