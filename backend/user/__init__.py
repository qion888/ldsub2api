"""Local authentication and multi-user administration package."""

from .auth import AuthService
from .errors import (
    AlreadyInitialized,
    AuthenticationRequired,
    Forbidden,
    InvalidCredentials,
    ResourceNotFound,
    SetupRequired,
    UserServiceError,
)

__all__ = [
    "AuthService",
    "AlreadyInitialized",
    "AuthenticationRequired",
    "Forbidden",
    "InvalidCredentials",
    "ResourceNotFound",
    "SetupRequired",
    "UserServiceError",
]

