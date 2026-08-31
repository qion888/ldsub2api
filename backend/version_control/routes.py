"""HTTP adapter for administrator version operations."""

from __future__ import annotations

import re
from typing import Any, Callable

from .errors import VersionControlError


VERSION_PATH = "/api/version"
VERSION_CHECK_PATH = "/api/version/check"
VERSION_UPDATE_PATH = "/api/version/update"
VERSION_BACKUP_PATH = "/api/version/backup"
VERSION_BACKUPS_PATH = "/api/version/backups"
VERSION_RESTORE_PATH = "/api/version/restore"
VERSION_ROLLBACK_PATH = "/api/version/rollback"
VERSION_BACKUP_DELETE_PATTERN = re.compile(r"^/api/version/backups/([A-Za-z0-9_-]{8,100})$")
VERSION_PATHS = frozenset({
    VERSION_PATH,
    VERSION_CHECK_PATH,
    VERSION_UPDATE_PATH,
    VERSION_BACKUP_PATH,
    VERSION_BACKUPS_PATH,
    VERSION_RESTORE_PATH,
    VERSION_ROLLBACK_PATH,
})
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
    backups_loader: Callable[[], dict[str, Any]] | None = None,
) -> bool:
    if path not in {VERSION_PATH, VERSION_BACKUPS_PATH}:
        return False
    if not _require_admin(principal, send_json):
        return True
    operation = version_info if path == VERSION_PATH else backups_loader
    if operation is None:
        return False
    return _run(send_json, operation)


def handle_post(
    path: str,
    data: dict[str, Any],
    *,
    send_json: SendJson,
    principal: dict[str, Any] | None,
    check_updates: Callable[[], dict[str, Any]],
    install_update: Callable[[], dict[str, Any]],
    create_backup: Callable[[str], dict[str, Any]] | None = None,
    restore_backup: Callable[[Any], dict[str, Any]] | None = None,
    rollback_update: Callable[..., dict[str, Any]] | None = None,
) -> bool:
    if path not in {VERSION_CHECK_PATH, VERSION_UPDATE_PATH}:
        if path not in {VERSION_BACKUP_PATH, VERSION_RESTORE_PATH, VERSION_ROLLBACK_PATH}:
            return False
    if not _require_admin(principal, send_json):
        return True
    if path == VERSION_CHECK_PATH:
        operation = check_updates
    elif path == VERSION_UPDATE_PATH:
        operation = install_update
    elif path == VERSION_BACKUP_PATH:
        if create_backup is None:
            return False
        operation = lambda: create_backup(str(data.get("reason") or "manual"))
    elif path == VERSION_RESTORE_PATH:
        if restore_backup is None:
            return False
        operation = lambda: restore_backup(data.get("backup_id"))
    else:
        if rollback_update is None:
            return False
        operation = lambda: rollback_update(
            data.get("backup_id"),
            restore_data=bool(data.get("restore_data")),
        )
    return _run(send_json, operation)


def handle_delete(
    path: str,
    *,
    send_json: SendJson,
    principal: dict[str, Any] | None,
    delete_backup: Callable[[str], dict[str, Any]] | None = None,
) -> bool:
    match = VERSION_BACKUP_DELETE_PATTERN.fullmatch(path)
    if not match:
        return False
    if not _require_admin(principal, send_json):
        return True
    if delete_backup is None:
        return False
    return _run(send_json, lambda: delete_backup(match.group(1)))
