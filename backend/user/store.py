"""SQLite schema and serialization helpers for local users.

The monitoring application already owns a SQLite database and uses additive
migrations.  This module follows the same pattern: all user tables are
created in the existing database and no separate server is required.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Iterable


VALID_ROLES = frozenset({"admin", "user"})
VALID_MODES = frozenset({"self_use", "external"})

DEFAULT_BASIC_SETTINGS: dict[str, Any] = {
    "site_name": "链动小铺本地监控台",
    "announcement": "",
    "contact_email": "",
    "timezone": "Asia/Shanghai",
    "base_url": "",
}
DEFAULT_SYSTEM_SETTINGS: dict[str, Any] = {
    "session_ttl_hours": 24,
    "maintenance_mode": False,
    "log_level": "info",
    # External installations can expose a read-only homepage without forcing
    # an authentication gate. Feature switches are deliberately separate so
    # ordinary users never inherit management access implicitly.
    "force_login": True,
    "allow_user_reclaim": False,
    "allow_user_sub2api_import": False,
}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    if column not in _table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def initialize_schema(database: Callable[[], sqlite3.Connection], *, now: Callable[[], str]) -> None:
    """Create user/session/install tables and seed deterministic defaults."""
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                revoked_at TEXT,
                user_agent TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS install_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                initialized INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'self_use',
                allow_registration INTEGER NOT NULL DEFAULT 0,
                initialized_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_sessions_user
                ON user_sessions(user_id, expires_at);
            CREATE INDEX IF NOT EXISTS idx_user_sessions_expiry
                ON user_sessions(expires_at, revoked_at);
            CREATE INDEX IF NOT EXISTS idx_users_role_enabled
                ON users(role, enabled);
            """
        )
        # Additive columns keep databases created by an early development
        # build readable without destructive migrations.
        _add_column(connection, "users", "display_name TEXT NOT NULL DEFAULT ''")
        _add_column(connection, "users", "role TEXT NOT NULL DEFAULT 'user'")
        _add_column(connection, "users", "enabled INTEGER NOT NULL DEFAULT 1")
        _add_column(connection, "users", "created_at TEXT")
        _add_column(connection, "users", "updated_at TEXT")
        _add_column(connection, "users", "last_login_at TEXT")
        stamp = str(now())
        connection.execute(
            """
            INSERT OR IGNORE INTO install_state(
                id, initialized, mode, allow_registration, initialized_at, updated_at
            ) VALUES(1, 0, 'self_use', 0, NULL, ?)
            """,
            (stamp,),
        )
        connection.execute(
            "UPDATE users SET created_at = COALESCE(created_at, ?), updated_at = COALESCE(updated_at, ?)",
            (stamp, stamp),
        )
        for key, value in (
            ("basic", DEFAULT_BASIC_SETTINGS),
            ("system", DEFAULT_SYSTEM_SETTINGS),
        ):
            connection.execute(
                "INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), stamp),
            )


def json_setting(connection: sqlite3.Connection, key: str, fallback: dict[str, Any]) -> dict[str, Any]:
    row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return dict(fallback)
    try:
        value = json.loads(str(row["value"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(fallback)
    return dict(value) if isinstance(value, dict) else dict(fallback)


def put_json_setting(connection: sqlite3.Connection, key: str, value: dict[str, Any], *, now: str) -> None:
    connection.execute(
        """
        INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), now),
    )


def installation(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM install_state WHERE id = 1").fetchone()
    if row is None:
        return {
            "initialized": False,
            "mode": "self_use",
            "allow_registration": False,
            "initialized_at": None,
            "updated_at": None,
        }
    mode = str(row["mode"] or "self_use").strip().lower()
    if mode not in VALID_MODES:
        mode = "self_use"
    return {
        "initialized": bool(row["initialized"]),
        "mode": mode,
        "allow_registration": bool(row["allow_registration"]),
        "initialized_at": row["initialized_at"],
        "updated_at": row["updated_at"],
    }


def set_installation(
    connection: sqlite3.Connection,
    *,
    initialized: bool,
    mode: str,
    allow_registration: bool,
    initialized_at: str | None,
    updated_at: str,
) -> None:
    if mode not in VALID_MODES:
        raise ValueError("invalid installation mode")
    connection.execute(
        """
        INSERT INTO install_state(
            id, initialized, mode, allow_registration, initialized_at, updated_at
        ) VALUES(1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            initialized = excluded.initialized,
            mode = excluded.mode,
            allow_registration = excluded.allow_registration,
            initialized_at = excluded.initialized_at,
            updated_at = excluded.updated_at
        """,
        (int(bool(initialized)), mode, int(bool(allow_registration)), initialized_at, updated_at),
    )


def user_from_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    role = str(row["role"] or "user").strip().lower()
    if role not in VALID_ROLES:
        role = "user"
    return {
        "id": int(row["id"]),
        "username": str(row["username"]),
        "display_name": str(row["display_name"] or row["username"] or ""),
        "role": role,
        "is_admin": role == "admin",
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def fetch_user_by_username(connection: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()


def fetch_user_by_id(connection: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()


def count_users(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def count_admins(connection: sqlite3.Connection, *, enabled_only: bool = False) -> int:
    query = "SELECT COUNT(*) FROM users WHERE role = 'admin'"
    if enabled_only:
        query += " AND enabled = 1"
    return int(connection.execute(query).fetchone()[0])


def list_user_rows(
    connection: sqlite3.Connection,
    *,
    search: str = "",
    role: str = "",
    enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[sqlite3.Row], int]:
    clauses: list[str] = []
    params: list[Any] = []
    if search:
        clauses.append("(username LIKE ? OR display_name LIKE ?)")
        pattern = f"%{search}%"
        params.extend([pattern, pattern])
    if role in VALID_ROLES:
        clauses.append("role = ?")
        params.append(role)
    if enabled is not None:
        clauses.append("enabled = ?")
        params.append(int(enabled))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    total = int(connection.execute(f"SELECT COUNT(*) FROM users{where}", params).fetchone()[0])
    offset = max(0, (int(page) - 1) * int(page_size))
    rows = connection.execute(
        f"SELECT * FROM users{where} ORDER BY id ASC LIMIT ? OFFSET ?",
        [*params, int(page_size), offset],
    ).fetchall()
    return rows, total


def cleanup_sessions(connection: sqlite3.Connection, *, now: str) -> int:
    cursor = connection.execute(
        "DELETE FROM user_sessions WHERE revoked_at IS NOT NULL OR expires_at <= ?",
        (now,),
    )
    return int(cursor.rowcount)


def session_row(connection: sqlite3.Connection, token_hash: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT s.*, u.username, u.display_name, u.role, u.enabled,
               u.created_at AS user_created_at, u.updated_at AS user_updated_at,
               u.last_login_at
        FROM user_sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ?
        """,
        (token_hash,),
    ).fetchone()
