"""HTTP adapter for administrator version operations."""

from __future__ import annotations

from typing import Any, Callable

from .errors import VersionControlError


VERSION_PATH = "/api/version"
VERSION_CHECK_PATH = "/api/version/check"
VERSION_UPDATE_PATH = "/api/version/update"
VERSION_PATHS = frozenset({VERSION_PATH, VERSION_CHECK_PATH, VERSION_UPDATE_PATH})
SendJson = Callable[[Any, int], Any]


def _require_admin(principal: dict[str, Any] | None, send_json: SendJson) -> bool:
    if principal is None:
        send_json({"detail": "请先登录", "code": "authentication_required"}, 401)
        return False
    if principal.get("role") != "admin":
        send_json({"detail": "需要管理员权限", "code": "forbidden"}, 403)
        return False
    return True


def _run(send_json: SendJson, operation: Callable[[], dict[str, Any]]) -> bool:
    try:
        send_json(operation(), 200)
    except VersionControlError as exc:
        send_json(exc.payload(), exc.status)
    except Exception:
        send_json(
            {
                "detail": "版本服务暂时不可用",
                "code": "version_service_error",
                "retryable": True,
            },
            500,
        )
    return True


def handle_get(
    path: str,
    *,
    send_json: SendJson,
    principal: dict[str, Any] | None,
    version_info: Callable[[], dict[str, Any]],
) -> bool:
    if path != VERSION_PATH:
        return False
    if not _require_admin(principal, send_json):
        return True
    return _run(send_json, version_info)


def handle_post(
    path: str,
    data: dict[str, Any],
    *,
    send_json: SendJson,
    principal: dict[str, Any] | None,
    check_updates: Callable[[], dict[str, Any]],
    install_update: Callable[[], dict[str, Any]],
) -> bool:
    del data
    if path not in {VERSION_CHECK_PATH, VERSION_UPDATE_PATH}:
        return False
    if not _require_admin(principal, send_json):
        return True
    operation = check_updates if path == VERSION_CHECK_PATH else install_update
    return _run(send_json, operation)
