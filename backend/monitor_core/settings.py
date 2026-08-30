"""Persistent application settings independent from HTTP routing."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlparse

DatabaseFactory = Callable[[], Any]


def json_value(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def read_checkout(database: DatabaseFactory) -> dict[str, Any]:
    with database() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = 'checkout'").fetchone()
        legacy = connection.execute("SELECT value FROM settings WHERE key = 'contact'").fetchone()
    fallback = json_value(legacy["value"] if legacy else None, {"contact": "", "note": ""})
    fallback.update({"query_password": "", "channel_id": 1})
    return json_value(row["value"] if row else None, fallback)


def read_json(database: DatabaseFactory, key: str, fallback: dict[str, Any]) -> dict[str, Any]:
    with database() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    value = json_value(row["value"] if row else None, fallback)
    return value if isinstance(value, dict) else dict(fallback)


def store_json(database: DatabaseFactory, key: str, value: dict[str, Any]) -> None:
    with database() as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def normalize_service_url(value: Any, default: str) -> str:
    candidate = str(value or default).strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("服务地址必须是 http 或 https URL")
    if len(candidate) > 300:
        raise ValueError("服务地址过长")
    return candidate
