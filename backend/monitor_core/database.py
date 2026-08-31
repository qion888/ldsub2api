"""SQLite connection and schema management for the monitoring backend."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable


class ManagedConnection(sqlite3.Connection):
    """Connection that releases its file handle when a with block exits."""

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def create_database(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10, factory=ManagedConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def initialize_database(
    database_factory: Callable[[], sqlite3.Connection],
    *,
    now: Callable[[], str],
    default_interval: int,
    default_redeem_url: str,
    default_sub2api_url: str,
    default_automation: dict,
) -> None:
    """Create tables and apply additive migrations without owning app globals."""
    with database_factory() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                name TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                watch_id INTEGER NOT NULL,
                title TEXT,
                price TEXT,
                stock TEXT,
                description TEXT,
                specs TEXT,
                fetched_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                token TEXT UNIQUE NOT NULL,
                name TEXT,
                keywords TEXT NOT NULL DEFAULT '',
                category_id INTEGER,
                category_name TEXT,
                goods_type TEXT NOT NULL DEFAULT 'card',
                enabled INTEGER NOT NULL DEFAULT 1,
                interval_seconds INTEGER NOT NULL DEFAULT 300,
                last_run TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shop_runs (
                id INTEGER PRIMARY KEY,
                shop_id INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                status TEXT NOT NULL,
                product_count INTEGER,
                error TEXT,
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS shop_products (
                shop_id INTEGER NOT NULL,
                goods_key TEXT NOT NULL,
                watch_id INTEGER NOT NULL,
                listed INTEGER NOT NULL DEFAULT 1,
                last_seen TEXT NOT NULL,
                PRIMARY KEY (shop_id, goods_key),
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS shop_exclusions (
                shop_id INTEGER NOT NULL,
                goods_key TEXT NOT NULL,
                removed_at TEXT NOT NULL,
                PRIMARY KEY (shop_id, goods_key),
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS preorders (
                id INTEGER PRIMARY KEY,
                watch_id INTEGER NOT NULL UNIQUE,
                quantity INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'watching',
                contact TEXT NOT NULL,
                query_password TEXT NOT NULL DEFAULT '',
                channel_id INTEGER NOT NULL DEFAULT 1,
                last_check TEXT,
                last_error TEXT,
                trade_no TEXT,
                payment_url TEXT,
                amount TEXT,
                created_at TEXT NOT NULL,
                triggered_at TEXT,
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_watch_time
                ON snapshots(watch_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_shop_runs_shop_time
                ON shop_runs(shop_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_preorders_status_check
                ON preorders(status, enabled, last_check);
            """
        )
        _add_column(connection, "watches", f"interval_seconds INTEGER NOT NULL DEFAULT {default_interval}")
        _add_column(connection, "watches", "created_at TEXT")
        _add_column(connection, "snapshots", "market_price TEXT")
        _add_column(connection, "snapshots", "image TEXT")
        _add_column(connection, "snapshots", "sale_status TEXT")
        _add_column(connection, "snapshots", "goods_key TEXT")
        _add_column(connection, "snapshots", "raw_data TEXT")
        _add_column(connection, "shops", "category_name TEXT")
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('contact', ?)",
            (json.dumps({"contact": "", "note": ""}, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('redeem', ?)",
            (json.dumps({"base_url": default_redeem_url}, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('sub2api', ?)",
            (json.dumps({"base_url": default_sub2api_url, "admin_key": ""}, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('sub2api_automation', ?)",
            (json.dumps(default_automation, ensure_ascii=False),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('sub2api_automation_state', ?)",
            (json.dumps({"last_run": None, "last_error": "", "last_result": None, "pending_card_codes": [], "reclaim_attempts": {}, "attempt_limited_card_codes": []}, ensure_ascii=False),),
        )
        connection.execute(
            "UPDATE watches SET created_at = COALESCE(created_at, ?) WHERE created_at IS NULL",
            (now(),),
        )
