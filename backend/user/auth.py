"""Authentication, installation, user administration, and settings service."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from typing import Any, Callable
from urllib.parse import urlparse

from . import security, store
from .errors import (
    AlreadyInitialized,
    AuthenticationRequired,
    Forbidden,
    InvalidCredentials,
    ResourceNotFound,
    SetupRequired,
    UserServiceError,
)


DatabaseFactory = Callable[[], Any]
NowFactory = Callable[[], str]

USERNAME_PATTERN = re.compile(r"^[\w.-]{3,64}$", re.UNICODE)
MAX_TOKEN_LENGTH = 512
DEFAULT_SESSION_TTL_HOURS = 24
MAX_SESSION_TTL_HOURS = 720


def _utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_username(value: Any) -> str:
    username = str(value or "").strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise UserServiceError(
            "用户名需为 3-64 个字母、数字、下划线、点或短横线",
            code="invalid_username",
        )
    return username


def normalize_password(value: Any) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 256:
        raise UserServiceError("密码长度需为 8-256 个字符", code="invalid_password")
    return value


def normalize_display_name(value: Any, fallback: str = "") -> str:
    display_name = str(value if value is not None else fallback).strip()
    if len(display_name) > 80:
        raise UserServiceError("显示名称过长", code="invalid_display_name")
    return display_name


def normalize_mode(value: Any, fallback: str = "self_use") -> str:
    mode = str(value or "").strip().lower()
    aliases = {
        "self_use": "self_use",
        "self": "self_use",
        "private": "self_use",
        "local": "self_use",
        "external": "external",
        "public": "external",
        "multi_user": "external",
        "multi-user": "external",
    }
    normalized = aliases.get(mode)
    if normalized:
        return normalized
    if fallback in store.VALID_MODES:
        return fallback
    raise UserServiceError("安装模式无效", code="invalid_mode")


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _page(value: Any, default: int, maximum: int) -> int:
    try:
        return min(max(int(value), 1), maximum)
    except (TypeError, ValueError):
        return default


class AuthService:
    """Stateful facade around the local SQLite user tables.

    The database callback is resolved for every operation so existing tests
    can replace ``main.database`` with an isolated connection factory.
    """

    def __init__(self, database: DatabaseFactory, *, now: NowFactory | None = None) -> None:
        self._database = database
        self._now = now or _now_iso

    def initialize(self) -> None:
        store.initialize_schema(self._database, now=self._now)

    def _ensure_schema(self) -> None:
        # ``run()`` initializes eagerly, while this guard makes health checks
        # and direct unit calls safe before the first server start.
        try:
            with self._database() as connection:
                connection.execute("SELECT 1 FROM install_state LIMIT 1").fetchone()
        except Exception as exc:
            if getattr(exc, "sqlite_errorname", "") not in {"SQLITE_ERROR", "SQLITE_SCHEMA"} and "no such table" not in str(exc).lower():
                raise
            self.initialize()

    def installation_status(self) -> dict[str, Any]:
        self._ensure_schema()
        try:
            with self._database() as connection:
                state = store.installation(connection)
                user_count = store.count_users(connection)
                admin_count = store.count_admins(connection, enabled_only=True)
                system = store.json_setting(connection, "system", store.DEFAULT_SYSTEM_SETTINGS)
                state = self._migrate_legacy_installation(
                    connection,
                    state,
                    user_count=user_count,
                    admin_count=admin_count,
                )
        except Exception as exc:
            if "no such table" not in str(exc).lower():
                raise
            return self._fallback_installation()
        initialized = bool(state["initialized"] and user_count > 0 and admin_count > 0)
        needs_setup = not initialized
        mode = normalize_mode(state.get("mode"), "self_use")
        allow_registration = bool(state.get("allow_registration"))
        if "allow_registration" in system:
            allow_registration = bool(state.get("allow_registration"))
        force_login = bool(system.get("force_login", True)) if mode == "external" else False
        return {
            "ok": True,
            "configured": not needs_setup,
            "initialized": initialized,
            "needs_setup": needs_setup,
            "mode": mode,
            "mode_label": "对外模式" if mode == "external" else "自用模式",
            "allow_registration": allow_registration,
            "auth_required": mode == "external" and initialized and force_login,
            "force_login": force_login,
            "allow_user_reclaim": bool(system.get("allow_user_reclaim", False)),
            "allow_user_sub2api_import": bool(system.get("allow_user_sub2api_import", False)),
            "user_count": user_count,
            "admin_count": admin_count,
            "initialized_at": state.get("initialized_at"),
        }

    @staticmethod
    def _fallback_installation() -> dict[str, Any]:
        return {
            "ok": True,
            "configured": False,
            "initialized": False,
            "needs_setup": True,
            "mode": "self_use",
            "mode_label": "自用模式",
            "allow_registration": False,
            "auth_required": False,
            "force_login": False,
            "allow_user_reclaim": False,
            "allow_user_sub2api_import": False,
            "user_count": 0,
            "admin_count": 0,
            "initialized_at": None,
        }

    def _system_settings(self, connection: Any) -> dict[str, Any]:
        value = store.json_setting(connection, "system", store.DEFAULT_SYSTEM_SETTINGS)
        merged = {**store.DEFAULT_SYSTEM_SETTINGS, **value}
        try:
            ttl = min(max(int(merged.get("session_ttl_hours", DEFAULT_SESSION_TTL_HOURS)), 1), MAX_SESSION_TTL_HOURS)
        except (TypeError, ValueError):
            ttl = DEFAULT_SESSION_TTL_HOURS
        merged["session_ttl_hours"] = ttl
        merged["maintenance_mode"] = _bool_value(merged.get("maintenance_mode"))
        level = str(merged.get("log_level") or "info").strip().lower()
        merged["log_level"] = level if level in {"debug", "info", "warning", "error"} else "info"
        return merged

    def _issue_session(self, connection: Any, user_row: Any, *, user_agent: str = "", ip_address: str = "") -> tuple[str, str]:
        token = security.new_session_token()
        stamp = str(self._now())
        system = self._system_settings(connection)
        current = _utc_datetime(stamp) or datetime.now(timezone.utc)
        expires = current + timedelta(hours=int(system["session_ttl_hours"]))
        connection.execute(
            """
            INSERT INTO user_sessions(
                token_hash, user_id, created_at, expires_at, last_seen_at,
                revoked_at, user_agent, ip_address
            ) VALUES(?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                security.token_digest(token),
                int(user_row["id"]),
                stamp,
                expires.isoformat(timespec="seconds"),
                stamp,
                str(user_agent or "")[:240],
                str(ip_address or "")[:80],
            ),
        )
        return token, expires.isoformat(timespec="seconds")

    def setup(self, payload: dict[str, Any], *, user_agent: str = "", ip_address: str = "") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UserServiceError("安装参数无效")
        username = normalize_username(payload.get("username"))
        password = normalize_password(payload.get("password"))
        display_name = normalize_display_name(payload.get("display_name"), username)
        mode = normalize_mode(payload.get("mode"), "self_use")
        allow_registration = _bool_value(payload.get("allow_registration"), default=False)
        force_login = _bool_value(payload.get("force_login"), default=mode == "external")
        allow_user_reclaim = _bool_value(payload.get("allow_user_reclaim"), default=False)
        allow_user_sub2api_import = _bool_value(payload.get("allow_user_sub2api_import"), default=False)
        if mode == "self_use":
            # Registration has no purpose in a trusted local installation.
            allow_registration = False
            force_login = False
            allow_user_reclaim = True
            allow_user_sub2api_import = True
        site_name = str(payload.get("site_name") or "").strip()[:120]
        timezone_name = str(payload.get("timezone") or "Asia/Shanghai").strip()[:64]

        self._ensure_schema()
        with self._database() as connection:
            # Serialize first-run setup so two browser tabs cannot create two
            # administrator accounts.
            connection.execute("BEGIN IMMEDIATE")
            state = store.installation(connection)
            if state["initialized"] or store.count_users(connection) > 0:
                raise AlreadyInitialized("系统已经完成安装")
            stamp = str(self._now())
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users(
                        username, password_hash, display_name, role, enabled,
                        created_at, updated_at, last_login_at
                    ) VALUES(?, ?, ?, 'admin', 1, ?, ?, NULL)
                    """,
                    (username, security.hash_password(password), display_name, stamp, stamp),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise AlreadyInitialized("管理员账号已经存在") from exc
                raise
            if site_name:
                basic = store.json_setting(connection, "basic", store.DEFAULT_BASIC_SETTINGS)
                basic["site_name"] = site_name
                basic["timezone"] = timezone_name or "Asia/Shanghai"
                store.put_json_setting(connection, "basic", basic, now=stamp)
            system = self._system_settings(connection)
            system["allow_registration"] = allow_registration
            system["force_login"] = force_login
            system["allow_user_reclaim"] = allow_user_reclaim
            system["allow_user_sub2api_import"] = allow_user_sub2api_import
            store.put_json_setting(connection, "system", system, now=stamp)
            store.set_installation(
                connection,
                initialized=True,
                mode=mode,
                allow_registration=allow_registration,
                initialized_at=stamp,
                updated_at=stamp,
            )
            user_row = store.fetch_user_by_id(connection, int(cursor.lastrowid))
            token, expires_at = self._issue_session(
                connection,
                user_row,
                user_agent=user_agent,
                ip_address=ip_address,
            )
        return {
            "ok": True,
            "token": token,
            "token_type": "Bearer",
            "expires_at": expires_at,
            "user": store.user_from_row(user_row),
            "mode": mode,
            "allow_registration": allow_registration,
            "auth_required": mode == "external" and force_login,
            "force_login": force_login,
            "allow_user_reclaim": allow_user_reclaim,
            "allow_user_sub2api_import": allow_user_sub2api_import,
            "installation": self.installation_status(),
        }

    def login(self, payload: dict[str, Any], *, user_agent: str = "", ip_address: str = "") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UserServiceError("登录参数无效")
        username = normalize_username(payload.get("username"))
        password = normalize_password(payload.get("password"))
        self._ensure_schema()
        with self._database() as connection:
            status = self._installation_from_connection(connection)
            if status["needs_setup"]:
                raise SetupRequired("请先完成安装引导")
            row = store.fetch_user_by_username(connection, username)
            if row is None or not security.verify_password(password, row["password_hash"]):
                raise InvalidCredentials("用户名或密码错误")
            if not bool(row["enabled"]):
                raise Forbidden("账号已停用", code="user_disabled")
            stamp = str(self._now())
            connection.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, int(row["id"])))
            row = store.fetch_user_by_id(connection, int(row["id"]))
            token, expires_at = self._issue_session(connection, row, user_agent=user_agent, ip_address=ip_address)
            mode = status["mode"]
        return {
            "ok": True,
            "authenticated": True,
            "token": token,
            "token_type": "Bearer",
            "expires_at": expires_at,
            "user": store.user_from_row(row),
            "mode": mode,
            "allow_registration": bool(status["allow_registration"]),
        }

    def register(self, payload: dict[str, Any], *, user_agent: str = "", ip_address: str = "") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UserServiceError("注册参数无效")
        username = normalize_username(payload.get("username"))
        password = normalize_password(payload.get("password"))
        display_name = normalize_display_name(payload.get("display_name"), username)
        self._ensure_schema()
        with self._database() as connection:
            status = self._installation_from_connection(connection)
            if status["needs_setup"]:
                raise SetupRequired("请先完成安装引导")
            if status["mode"] != "external" or not status["allow_registration"]:
                raise Forbidden("当前未开放注册", code="registration_disabled")
            stamp = str(self._now())
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users(
                        username, password_hash, display_name, role, enabled,
                        created_at, updated_at, last_login_at
                    ) VALUES(?, ?, ?, 'user', 1, ?, ?, NULL)
                    """,
                    (username, security.hash_password(password), display_name, stamp, stamp),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise UserServiceError("用户名已存在", status=409, code="username_taken") from exc
                raise
            row = store.fetch_user_by_id(connection, int(cursor.lastrowid))
            token, expires_at = self._issue_session(connection, row, user_agent=user_agent, ip_address=ip_address)
        return {
            "ok": True,
            "authenticated": True,
            "token": token,
            "token_type": "Bearer",
            "expires_at": expires_at,
            "user": store.user_from_row(row),
            "mode": "external",
            "allow_registration": True,
        }

    def _installation_from_connection(self, connection: Any) -> dict[str, Any]:
        state = store.installation(connection)
        users = store.count_users(connection)
        admins = store.count_admins(connection, enabled_only=True)
        state = self._migrate_legacy_installation(
            connection,
            state,
            user_count=users,
            admin_count=admins,
        )
        system = self._system_settings(connection)
        initialized = bool(state["initialized"] and users > 0 and admins > 0)
        return {
            "initialized": initialized,
            "needs_setup": not initialized,
            "mode": normalize_mode(state.get("mode"), "self_use"),
            "allow_registration": bool(state.get("allow_registration")),
            "user_count": users,
            "admin_count": admins,
            "system": system,
            "initialized_at": state.get("initialized_at"),
        }

    def _migrate_legacy_installation(
        self,
        connection: Any,
        state: dict[str, Any],
        *,
        user_count: int,
        admin_count: int,
    ) -> dict[str, Any]:
        """Recognize databases seeded before ``install_state`` was introduced.

        An enabled administrator is an unambiguous signal that setup already
        happened.  Persisting the marker keeps the setup endpoint from asking
        for a second administrator on every restart while preserving the
        existing mode and registration flag.
        """
        if state.get("initialized") or user_count <= 0 or admin_count <= 0:
            return state
        stamp = str(state.get("updated_at") or self._now())
        initialized_at = state.get("initialized_at") or stamp
        store.set_installation(
            connection,
            initialized=True,
            mode=normalize_mode(state.get("mode"), "self_use"),
            allow_registration=bool(state.get("allow_registration")),
            initialized_at=initialized_at,
            updated_at=stamp,
        )
        return store.installation(connection)

    @staticmethod
    def _extract_token(headers: Any) -> str:
        if headers is None:
            return ""
        authorization = ""
        try:
            authorization = str(headers.get("Authorization", ""))
        except AttributeError:
            pass
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                token = value.strip()
                return token[:MAX_TOKEN_LENGTH]
        try:
            token = str(headers.get("X-Session-Token", "")).strip()
        except AttributeError:
            token = ""
        if token:
            return token[:MAX_TOKEN_LENGTH]
        try:
            raw_cookie = str(headers.get("Cookie", ""))
        except AttributeError:
            raw_cookie = ""
        if raw_cookie:
            cookie = SimpleCookie()
            try:
                cookie.load(raw_cookie)
                return str(cookie.get("ldxp_session").value if cookie.get("ldxp_session") else "")[:MAX_TOKEN_LENGTH]
            except (CookieError, ValueError, KeyError, TypeError):
                return ""
        return ""

    def authenticate(self, headers: Any) -> dict[str, Any] | None:
        token = self._extract_token(headers)
        if not token:
            return None
        self._ensure_schema()
        stamp = str(self._now())
        current = _utc_datetime(stamp) or datetime.now(timezone.utc)
        with self._database() as connection:
            row = store.session_row(connection, security.token_digest(token))
            if row is None or not bool(row["enabled"]):
                return None
            expires = _utc_datetime(row["expires_at"])
            if expires is None or expires <= current or row["revoked_at"]:
                return None
            connection.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE id = ?",
                (stamp, int(row["id"])),
            )
            role = str(row["role"] or "user").lower()
            role = role if role in store.VALID_ROLES else "user"
            return {
                "id": int(row["user_id"]),
                "username": str(row["username"]),
                "display_name": str(row["display_name"] or row["username"]),
                "role": role,
                "is_admin": role == "admin",
                "enabled": True,
                "created_at": row["user_created_at"],
                "updated_at": row["user_updated_at"],
                "last_login_at": row["last_login_at"],
                "session_id": int(row["id"]),
                "expires_at": row["expires_at"],
            }

    def require(self, headers: Any, *, admin: bool = False) -> dict[str, Any]:
        principal = self.authenticate(headers)
        if principal is None:
            raise AuthenticationRequired("请先登录")
        if admin and principal.get("role") != "admin":
            raise Forbidden("需要管理员权限")
        return principal

    def logout(self, headers: Any) -> dict[str, Any]:
        token = self._extract_token(headers)
        if token:
            self._ensure_schema()
            with self._database() as connection:
                connection.execute(
                    "UPDATE user_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                    (str(self._now()), security.token_digest(token)),
                )
        return {"ok": True}

    def me(self, headers: Any) -> dict[str, Any]:
        status = self.installation_status()
        principal = self.authenticate(headers)
        return {
            "ok": True,
            "authenticated": principal is not None,
            "user": principal,
            "mode": status["mode"],
            "needs_setup": status["needs_setup"],
            "configured": status["configured"],
            "allow_registration": status["allow_registration"],
            "expires_at": principal.get("expires_at") if principal else None,
        }

    def settings(self) -> dict[str, Any]:
        self._ensure_schema()
        with self._database() as connection:
            status = self._installation_from_connection(connection)
            basic = {**store.DEFAULT_BASIC_SETTINGS, **store.json_setting(connection, "basic", store.DEFAULT_BASIC_SETTINGS)}
            system = {**store.DEFAULT_SYSTEM_SETTINGS, **status["system"]}
        return {
            "ok": True,
            "mode": status["mode"],
            "mode_label": "对外模式" if status["mode"] == "external" else "自用模式",
            "allow_registration": bool(status["allow_registration"]),
            "auth_required": status["mode"] == "external" and status["initialized"] and bool(status["system"].get("force_login", True)),
            "force_login": bool(status["system"].get("force_login", True)) if status["mode"] == "external" else False,
            "allow_user_reclaim": bool(status["system"].get("allow_user_reclaim", False)),
            "allow_user_sub2api_import": bool(status["system"].get("allow_user_sub2api_import", False)),
            "basic": basic,
            "system": system,
        }

    def update_settings(self, payload: dict[str, Any], *, section: str | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UserServiceError("设置参数无效")
        self._ensure_schema()
        section_name = str(section or "").strip().lower()
        if section_name not in {"", "basic", "system"}:
            raise UserServiceError("设置分区无效")
        with self._database() as connection:
            status = self._installation_from_connection(connection)
            stamp = str(self._now())
            if section_name in {"", "basic"}:
                incoming = payload.get("basic") if section_name == "" else payload
                if isinstance(incoming, dict):
                    basic = {**store.DEFAULT_BASIC_SETTINGS, **store.json_setting(connection, "basic", store.DEFAULT_BASIC_SETTINGS)}
                    for key, limit in (
                        ("site_name", 120),
                        ("announcement", 1000),
                        ("contact_email", 160),
                        ("timezone", 64),
                        ("base_url", 300),
                    ):
                        if key in incoming:
                            value = str(incoming.get(key) or "").strip()
                            if len(value) > limit:
                                raise UserServiceError(f"{key} 设置过长", code="invalid_setting")
                            if key == "base_url" and value:
                                parsed_url = urlparse(value)
                                if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                                    raise UserServiceError("服务地址必须是有效的 HTTP(S) URL", code="invalid_setting")
                                value = value.rstrip("/")
                            basic[key] = value
                    store.put_json_setting(connection, "basic", basic, now=stamp)
            if section_name in {"", "system"}:
                incoming = payload.get("system") if section_name == "" else payload
                if isinstance(incoming, dict):
                    system = self._system_settings(connection)
                    if "session_ttl_hours" in incoming:
                        try:
                            ttl = int(incoming["session_ttl_hours"])
                        except (TypeError, ValueError) as exc:
                            raise UserServiceError("会话有效期无效", code="invalid_setting") from exc
                        if not 1 <= ttl <= MAX_SESSION_TTL_HOURS:
                            raise UserServiceError("会话有效期需为 1-720 小时", code="invalid_setting")
                        system["session_ttl_hours"] = ttl
                    for key in ("maintenance_mode",):
                        if key in incoming:
                            system[key] = _bool_value(incoming[key])
                    if "log_level" in incoming:
                        level = str(incoming.get("log_level") or "info").strip().lower()
                        if level not in {"debug", "info", "warning", "error"}:
                            raise UserServiceError("日志级别无效", code="invalid_setting")
                        system["log_level"] = level
                    if "force_login" in incoming:
                        system["force_login"] = _bool_value(incoming["force_login"], default=True)
                    if "allow_user_reclaim" in incoming:
                        system["allow_user_reclaim"] = _bool_value(incoming["allow_user_reclaim"])
                    if "allow_user_sub2api_import" in incoming:
                        system["allow_user_sub2api_import"] = _bool_value(incoming["allow_user_sub2api_import"])
                    raw_mode = incoming.get("mode", payload.get("mode"))
                    mode = normalize_mode(raw_mode, status["mode"]) if raw_mode is not None else status["mode"]
                    allow_registration = _bool_value(
                        incoming.get("allow_registration", payload.get("allow_registration")),
                        default=status["allow_registration"],
                    )
                    if mode == "external" and status["mode"] != "external":
                        # Do not carry self-use guest capabilities into a
                        # multi-user installation unless the administrator
                        # explicitly supplies the external switches.
                        if "force_login" not in incoming:
                            system["force_login"] = True
                        if "allow_user_reclaim" not in incoming:
                            system["allow_user_reclaim"] = False
                        if "allow_user_sub2api_import" not in incoming:
                            system["allow_user_sub2api_import"] = False
                    if mode == "self_use":
                        allow_registration = False
                        system["force_login"] = False
                        # Self-use guests are intentionally allowed to use the
                        # local recovery/import workflow, regardless of the
                        # external ordinary-user switches.
                        system["allow_user_reclaim"] = True
                        system["allow_user_sub2api_import"] = True
                    system["allow_registration"] = allow_registration
                    store.put_json_setting(connection, "system", system, now=stamp)
                    store.set_installation(
                        connection,
                        initialized=status["initialized"],
                        mode=mode,
                        allow_registration=allow_registration,
                        initialized_at=status.get("initialized_at"),
                        updated_at=stamp,
                    )
        return self.settings()

    def list_users(self, query: dict[str, Any] | None = None) -> dict[str, Any]:
        query = query or {}
        page = _page(query.get("page", 1), 1, 10_000)
        page_size = _page(query.get("page_size", 20), 20, 100)
        search = str(query.get("search", "") or "").strip()[:80]
        role = str(query.get("role", "") or "").strip().lower()
        enabled_value = query.get("enabled")
        enabled: bool | None = None
        if enabled_value not in (None, "", "all"):
            enabled = _bool_value(enabled_value)
        self._ensure_schema()
        with self._database() as connection:
            rows, total = store.list_user_rows(
                connection,
                search=search,
                role=role,
                enabled=enabled,
                page=page,
                page_size=page_size,
            )
        pages = max(1, (total + page_size - 1) // page_size)
        return {
            "ok": True,
            "items": [store.user_from_row(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def get_user(self, user_id: Any) -> dict[str, Any]:
        try:
            normalized_id = int(user_id)
        except (TypeError, ValueError) as exc:
            raise ResourceNotFound("用户不存在") from exc
        self._ensure_schema()
        with self._database() as connection:
            row = store.fetch_user_by_id(connection, normalized_id)
        user = store.user_from_row(row)
        if user is None:
            raise ResourceNotFound("用户不存在")
        return {"ok": True, "user": user}

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UserServiceError("用户参数无效")
        username = normalize_username(payload.get("username"))
        password = normalize_password(payload.get("password"))
        display_name = normalize_display_name(payload.get("display_name"), username)
        role = str(payload.get("role") or "user").strip().lower()
        if role not in store.VALID_ROLES:
            raise UserServiceError("用户角色无效", code="invalid_role")
        enabled = _bool_value(payload.get("enabled"), default=True)
        self._ensure_schema()
        with self._database() as connection:
            stamp = str(self._now())
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users(
                        username, password_hash, display_name, role, enabled,
                        created_at, updated_at, last_login_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (username, security.hash_password(password), display_name, role, int(enabled), stamp, stamp),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise UserServiceError("用户名已存在", status=409, code="username_taken") from exc
                raise
            row = store.fetch_user_by_id(connection, int(cursor.lastrowid))
        return {"ok": True, "user": store.user_from_row(row)}

    def update_user(self, user_id: Any, payload: dict[str, Any], *, actor_id: int | None = None) -> dict[str, Any]:
        try:
            target_id = int(user_id)
        except (TypeError, ValueError) as exc:
            raise ResourceNotFound("用户不存在") from exc
        if not isinstance(payload, dict):
            raise UserServiceError("用户参数无效")
        self._ensure_schema()
        with self._database() as connection:
            row = store.fetch_user_by_id(connection, target_id)
            if row is None:
                raise ResourceNotFound("用户不存在")
            role = str(payload.get("role", row["role"])).strip().lower()
            if role not in store.VALID_ROLES:
                raise UserServiceError("用户角色无效", code="invalid_role")
            enabled = _bool_value(payload.get("enabled"), default=bool(row["enabled"]))
            if target_id == actor_id and (role != "admin" or not enabled):
                raise UserServiceError("不能停用或降级当前管理员", status=409, code="self_lockout")
            if str(row["role"]).lower() == "admin" and (role != "admin" or not enabled):
                if store.count_admins(connection, enabled_only=True) <= 1:
                    raise UserServiceError("至少需要保留一个启用的管理员", status=409, code="last_admin")
            display_name = normalize_display_name(payload.get("display_name"), row["display_name"] or row["username"])
            username = normalize_username(payload.get("username", row["username"]))
            stamp = str(self._now())
            try:
                connection.execute(
                    "UPDATE users SET username = ?, display_name = ?, role = ?, enabled = ?, updated_at = ? WHERE id = ?",
                    (username, display_name, role, int(enabled), stamp, target_id),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise UserServiceError("用户名已存在", status=409, code="username_taken") from exc
                raise
            updated = store.fetch_user_by_id(connection, target_id)
        return {"ok": True, "user": store.user_from_row(updated)}

    def set_password(self, user_id: Any, payload: dict[str, Any], *, actor: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            target_id = int(user_id)
        except (TypeError, ValueError) as exc:
            raise ResourceNotFound("用户不存在") from exc
        if not isinstance(payload, dict):
            raise UserServiceError("密码参数无效")
        new_password = normalize_password(payload.get("password") or payload.get("new_password"))
        if actor is not None and int(actor.get("id", -1)) == target_id and actor.get("role") != "admin":
            current = normalize_password(payload.get("current_password"))
        else:
            current = None
        self._ensure_schema()
        with self._database() as connection:
            row = store.fetch_user_by_id(connection, target_id)
            if row is None:
                raise ResourceNotFound("用户不存在")
            if current is not None and not security.verify_password(current, row["password_hash"]):
                raise InvalidCredentials("当前密码错误")
            stamp = str(self._now())
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (security.hash_password(new_password), stamp, target_id),
            )
            # Revoking all existing sessions after a password change avoids
            # leaving a forgotten browser logged in.
            connection.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (stamp, target_id),
            )
        return {"ok": True, "user_id": target_id}

    def delete_user(self, user_id: Any, *, actor_id: int | None = None) -> dict[str, Any]:
        try:
            target_id = int(user_id)
        except (TypeError, ValueError) as exc:
            raise ResourceNotFound("用户不存在") from exc
        if target_id == actor_id:
            raise UserServiceError("不能删除当前登录账号", status=409, code="self_delete")
        self._ensure_schema()
        with self._database() as connection:
            row = store.fetch_user_by_id(connection, target_id)
            if row is None:
                raise ResourceNotFound("用户不存在")
            if str(row["role"]).lower() == "admin" and bool(row["enabled"]) and store.count_admins(connection, enabled_only=True) <= 1:
                raise UserServiceError("至少需要保留一个启用的管理员", status=409, code="last_admin")
            connection.execute("DELETE FROM users WHERE id = ?", (target_id,))
        return {"ok": True, "user_id": target_id}
