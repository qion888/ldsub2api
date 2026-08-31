"""Typed errors shared by the user service and HTTP route adapter."""

from __future__ import annotations

from typing import Any


class UserServiceError(Exception):
    status = 400
    code = "invalid_request"

    def __init__(self, detail: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(detail)
        self.detail = str(detail)
        if status is not None:
            self.status = int(status)
        if code is not None:
            self.code = str(code)

    def payload(self) -> dict[str, Any]:
        return {"detail": self.detail, "code": self.code}


class AuthenticationRequired(UserServiceError):
    status = 401
    code = "auth_required"


class InvalidCredentials(UserServiceError):
    status = 401
    code = "invalid_credentials"


class Forbidden(UserServiceError):
    status = 403
    code = "forbidden"


class SetupRequired(UserServiceError):
    status = 409
    code = "setup_required"


class AlreadyInitialized(UserServiceError):
    status = 409
    code = "already_initialized"


class ResourceNotFound(UserServiceError):
    status = 404
    code = "not_found"

