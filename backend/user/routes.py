"""HTTP adapter and authorization policy for the user subsystem."""

from __future__ import annotations

import re
from typing import Any, Callable

from .auth import AuthService
from .errors import AuthenticationRequired, Forbidden, SetupRequired, UserServiceError


SendJson = Callable[[Any, int], Any]

INSTALL_STATUS_PATH = "/api/install/status"
INSTALL_SETUP_PATH = "/api/install/setup"
AUTH_STATUS_PATH = "/api/auth/status"
AUTH_SETUP_PATH = "/api/auth/setup"
AUTH_LOGIN_PATH = "/api/auth/login"
AUTH_LOGOUT_PATH = "/api/auth/logout"
AUTH_REGISTER_PATH = "/api/auth/register"
AUTH_ME_PATH = "/api/auth/me"
AUTH_PASSWORD_PATH = "/api/auth/password"
SETTINGS_PATH = "/api/settings"
SETTINGS_BASIC_PATH = "/api/settings/basic"
SETTINGS_SYSTEM_PATH = "/api/settings/system"
USERS_PATH = "/api/users"

_USER_ID = r"/api/users/(\d+)"
_USER_PASSWORD = r"/api/users/(\d+)/password"
_WATCH_HISTORY = re.compile(r"/api/watches/\d+/history$")
_SHOP_PRODUCTS = re.compile(r"/api/shops/\d+/products$")
_CARD_IMPORT_RECORD = re.compile(r"/api/sub2api/card-import-records(?:/\d+)?(?:/retry)?$")
_VERSION_BACKUP_DELETE = re.compile(r"/api/version/backups/[A-Za-z0-9_-]{8,100}$")
_VERSION_PATHS = frozenset({
    "/api/version",
    "/api/version/check",
    "/api/version/update",
    "/api/version/backup",
    "/api/version/backups",
    "/api/version/restore",
    "/api/version/rollback",
})

AUTH_BOOTSTRAP_GET = frozenset({
    "/api/health",
    INSTALL_STATUS_PATH,
    AUTH_STATUS_PATH,
    AUTH_ME_PATH,
})
AUTH_BOOTSTRAP_POST = frozenset({
    INSTALL_SETUP_PATH,
    AUTH_SETUP_PATH,
    AUTH_LOGIN_PATH,
    AUTH_REGISTER_PATH,
})

USER_RECLAIM_EXACT = frozenset({
    "/api/sub2api/reclaim-401",
    "/api/sub2api/reclaim401",
    "/api/sub2api/reclaim-401/retry",
    "/api/sub2api/reclaim401/retry",
    "/api/sub2api/reclaim-progress",
    "/api/redeem/health-check",
    "/api/redeem/reclaim",
    "/api/redeem/progress",
    "/api/redeem/download",
    "/api/redeem/config",
})
USER_IMPORT_EXACT = frozenset({
    "/api/sub2api/import",
    "/api/sub2api/options",
    "/api/sub2api/test",
    "/api/sub2api/config",
    "/api/sub2api/automation",
})


def _self_use_guest_legacy(method: str, path: str) -> bool:
    """Keep local legacy APIs open while retaining the new admin boundary."""
    method = method.upper()
    if path in {USERS_PATH, AUTH_PASSWORD_PATH, SETTINGS_SYSTEM_PATH} or path in _VERSION_PATHS or _VERSION_BACKUP_DELETE.fullmatch(path):
        return False
    if re.fullmatch(_USER_ID, path) or re.fullmatch(_USER_PASSWORD, path):
        return False
    if method in {"POST", "PUT", "PATCH", "DELETE"} and path in {
        SETTINGS_PATH,
        SETTINGS_BASIC_PATH,
        SETTINGS_SYSTEM_PATH,
    }:
        return False
    return True

PUBLIC_GET_EXACT = frozenset(
    {
        "/api/health",
        INSTALL_STATUS_PATH,
        AUTH_STATUS_PATH,
        AUTH_ME_PATH,
        "/api/watches",
        "/api/shops",
        "/api/preorders",
        "/api/pay/juuid",
        "/api/pay/channels",
    }
)
PUBLIC_POST_EXACT = frozenset(
    {
        INSTALL_SETUP_PATH,
        AUTH_SETUP_PATH,
        AUTH_LOGIN_PATH,
        AUTH_REGISTER_PATH,
        "/api/checkout/prepare",
        "/api/pay/order",
        "/api/mock/pay/order",
        "/api/order-query/search",
        "/api/order-query/detail",
        "/api/order-query/waf-verification/start",
        "/api/order-query/waf-verification/complete",
        "/api/order-query/complaints/preview",
        "/api/order-query/complaints/context",
        "/api/order-query/complaints/history",
        "/api/order-query/complaints/upload",
        "/api/order-query/complaints/submit",
        "/api/order-query/complaints/upload/remove",
    }
)


def _is_public(method: str, path: str) -> bool:
    method = method.upper()
    if method == "OPTIONS":
        return True
    if method == "GET":
        return path in PUBLIC_GET_EXACT or bool(_WATCH_HISTORY.fullmatch(path)) or bool(_SHOP_PRODUCTS.fullmatch(path))
    if method == "POST":
        return path in PUBLIC_POST_EXACT
    return False


def _is_user_allowed(method: str, path: str) -> bool:
    """Operations available to a signed-in ordinary user in external mode."""
    method = method.upper()
    if method == "GET" and path in {
        "/api/settings",
        SETTINGS_BASIC_PATH,
        "/api/settings/contact",
        "/api/settings/checkout",
    }:
        return True
    if method == "PUT" and path in {"/api/settings/contact", "/api/settings/checkout"}:
        return True
    if method == "POST" and path in {AUTH_LOGOUT_PATH, AUTH_PASSWORD_PATH}:
        return True
    return False


def _user_feature_allowed(method: str, path: str, installation: dict[str, Any]) -> bool:
    """Allow explicitly enabled external-user recovery/import operations."""
    method = method.upper()
    if bool(installation.get("allow_user_reclaim")) and path in USER_RECLAIM_EXACT:
        return method in {"GET", "POST"}
    if bool(installation.get("allow_user_sub2api_import")):
        if path in USER_IMPORT_EXACT and method in {"GET", "POST"}:
            return True
        if _CARD_IMPORT_RECORD.fullmatch(path) and method in {"GET", "POST"}:
            return True
    return False


def authorization(
    method: str,
    path: str,
    *,
    installation: dict[str, Any],
    principal: dict[str, Any] | None,
) -> None:
    """Raise a typed error when a request is not allowed by the mode/role."""
    mode = str(installation.get("mode") or "self_use")
    # A local/self-use installation keeps the legacy purchase profile usable
    # for a guest browser.  These values are local-only and do not grant any
    # management or account privileges.
    if mode == "self_use" and method.upper() in {"GET", "PUT"} and path in {
        "/api/settings/contact",
        "/api/settings/checkout",
    }:
        return
    # A fresh database is intentionally left compatible with the legacy local
    # monitor API until the first administrator completes the wizard.  Once an
    # installation exists, both modes use the same management boundary.
    initialized = bool(installation.get("initialized") and not installation.get("needs_setup"))
    if not initialized:
        if path in {INSTALL_STATUS_PATH, AUTH_STATUS_PATH, INSTALL_SETUP_PATH, AUTH_SETUP_PATH, "/api/health"}:
            return
        if mode == "self_use":
            return
        if installation.get("needs_setup"):
            raise SetupRequired("请先完成安装引导")
    # Self-use is a trusted local workflow.  Keep legacy monitoring, purchase,
    # redeem, and Sub2API endpoints usable for a guest; the user adapter still
    # enforces authentication for account CRUD, password, and system settings.
    if mode == "self_use" and principal is None and _self_use_guest_legacy(method, path):
        return
    # External mode can expose the public homepage while still protecting
    # every other API route behind login. Authentication bootstrap endpoints
    # remain public so the login/install screens can render.
    if initialized and mode == "external" and bool(installation.get("force_login", installation.get("auth_required", True))) and principal is None:
        method_upper = method.upper()
        if not (
            (method_upper == "GET" and path in AUTH_BOOTSTRAP_GET)
            or (method_upper == "POST" and path in AUTH_BOOTSTRAP_POST)
        ):
            raise AuthenticationRequired("璇峰厛鐧诲綍")
    if _is_public(method, path):
        return
    if principal is None:
        raise AuthenticationRequired("请先登录")
    if principal.get("role") == "admin":
        return
    if _user_feature_allowed(method, path, installation):
        return
    if _is_user_allowed(method, path):
        return
    raise Forbidden("需要管理员权限")


def _emit_error(send_json: SendJson, error: UserServiceError) -> None:
    send_json(error.payload(), error.status)


def _required_principal(
    service: AuthService,
    headers: Any,
    send_json: SendJson,
    *,
    admin: bool = False,
) -> dict[str, Any] | None:
    try:
        return service.require(headers, admin=admin)
    except UserServiceError as exc:
        _emit_error(send_json, exc)
        return None


def _run(send_json: SendJson, operation: Callable[[], Any]) -> bool:
    try:
        value = operation()
        send_json(value, 200)
    except UserServiceError as exc:
        _emit_error(send_json, exc)
    except Exception as exc:
        # Do not expose SQL or password details to a remote caller.
        send_json({"detail": "用户服务暂时不可用", "code": "user_service_error"}, 500)
    return True


def handle_get(
    path: str,
    *,
    send_json: SendJson,
    service: AuthService,
    headers: Any = None,
    principal: dict[str, Any] | None = None,
    query_values: dict[str, list[str]] | None = None,
) -> bool:
    if path in {INSTALL_STATUS_PATH, AUTH_STATUS_PATH}:
        return _run(send_json, service.installation_status)
    if path in {INSTALL_SETUP_PATH, AUTH_SETUP_PATH}:
        return _run(send_json, service.installation_status)
    if path == AUTH_ME_PATH:
        return _run(send_json, lambda: service.me(headers))
    if path in {SETTINGS_PATH, SETTINGS_BASIC_PATH, SETTINGS_SYSTEM_PATH}:
        def read_settings() -> dict[str, Any]:
            if path == SETTINGS_SYSTEM_PATH:
                service.require(headers, admin=True)
            value = service.settings()
            # System settings are administrator-only.  Keep the aggregate
            # endpoint useful for ordinary users without leaking operational
            # controls such as session TTL or maintenance state.
            is_admin = bool(principal and principal.get("role") == "admin")
            if path == SETTINGS_PATH and not is_admin:
                return {
                    "ok": True,
                    "mode": value["mode"],
                    "mode_label": value["mode_label"],
                    "allow_registration": value["allow_registration"],
                    "auth_required": value["auth_required"],
                    "basic": value["basic"],
                }
            if path == SETTINGS_BASIC_PATH:
                return {"ok": True, "mode": value["mode"], "basic": value["basic"]}
            if path == SETTINGS_SYSTEM_PATH:
                return {"ok": True, "mode": value["mode"], "allow_registration": value["allow_registration"], "system": value["system"]}
            return value
        return _run(send_json, read_settings)
    if path == USERS_PATH:
        if _required_principal(service, headers, send_json, admin=True) is None:
            return True
        query: dict[str, Any] = {}
        for key, values in (query_values or {}).items():
            if values:
                query[key] = values[0]
        return _run(send_json, lambda: service.list_users(query))
    user_match = re.fullmatch(_USER_ID, path)
    if user_match:
        if _required_principal(service, headers, send_json, admin=True) is None:
            return True
        return _run(send_json, lambda: service.get_user(int(user_match.group(1))))
    return False


def handle_post(
    path: str,
    data: dict[str, Any],
    *,
    send_json: SendJson,
    service: AuthService,
    headers: Any = None,
    user_agent: str = "",
    ip_address: str = "",
) -> bool:
    if path in {INSTALL_SETUP_PATH, AUTH_SETUP_PATH}:
        return _run(send_json, lambda: service.setup(data, user_agent=user_agent, ip_address=ip_address))
    if path == AUTH_LOGIN_PATH:
        return _run(send_json, lambda: service.login(data, user_agent=user_agent, ip_address=ip_address))
    if path == AUTH_REGISTER_PATH:
        return _run(send_json, lambda: service.register(data, user_agent=user_agent, ip_address=ip_address))
    if path == AUTH_LOGOUT_PATH:
        return _run(send_json, lambda: service.logout(headers))
    if path == AUTH_PASSWORD_PATH:
        def change_own_password() -> dict[str, Any]:
            principal = service.require(headers)
            return service.set_password(principal["id"], data, actor=principal)
        return _run(send_json, change_own_password)
    if path == USERS_PATH:
        if _required_principal(service, headers, send_json, admin=True) is None:
            return True
        return _run(send_json, lambda: service.create_user(data))
    user_password_match = re.fullmatch(_USER_PASSWORD, path)
    if user_password_match:
        user_id = int(user_password_match.group(1))
        def change_user_password() -> dict[str, Any]:
            principal = service.require(headers, admin=True)
            return service.set_password(user_id, data, actor=principal)
        return _run(send_json, change_user_password)
    return False


def handle_put(
    path: str,
    data: dict[str, Any],
    *,
    send_json: SendJson,
    service: AuthService,
    headers: Any = None,
) -> bool:
    if path == SETTINGS_PATH:
        if _required_principal(service, headers, send_json, admin=True) is None:
            return True
        return _run(send_json, lambda: service.update_settings(data))
    if path == SETTINGS_BASIC_PATH:
        if _required_principal(service, headers, send_json, admin=True) is None:
            return True
        return _run(send_json, lambda: service.update_settings(data, section="basic"))
    if path == SETTINGS_SYSTEM_PATH:
        if _required_principal(service, headers, send_json, admin=True) is None:
            return True
        return _run(send_json, lambda: service.update_settings(data, section="system"))
    user_match = re.fullmatch(_USER_ID, path)
    if user_match:
        principal = _required_principal(service, headers, send_json, admin=True)
        if principal is None:
            return True
        actor_id = int(principal["id"]) if principal else None
        return _run(send_json, lambda: service.update_user(int(user_match.group(1)), data, actor_id=actor_id))
    return False


def handle_delete(
    path: str,
    *,
    send_json: SendJson,
    service: AuthService,
    headers: Any = None,
) -> bool:
    user_match = re.fullmatch(_USER_ID, path)
    if not user_match:
        return False
    principal = _required_principal(service, headers, send_json, admin=True)
    if principal is None:
        return True
    actor_id = int(principal["id"]) if principal else None
    return _run(send_json, lambda: service.delete_user(int(user_match.group(1)), actor_id=actor_id))
