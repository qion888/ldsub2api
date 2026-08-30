"""Persistent audit records for direct card verification and Sub2API pushes."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable


DatabaseFactory = Callable[[], sqlite3.Connection]
STATUSES = {"running", "pending", "success", "failed"}
STAGES = {"verify", "reclaim", "poll", "download", "stage", "ready", "waiting", "push", "done", "error"}
COUNT_FIELDS = (
    "card_count",
    "verified_count",
    "downloaded_files",
    "account_count",
    "success_count",
    "failed_count",
)


class RecordNotFound(LookupError):
    pass


def initialize(database_factory: DatabaseFactory) -> None:
    with database_factory() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sub2api_card_import_records (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                card_count INTEGER NOT NULL DEFAULT 0,
                verified_count INTEGER NOT NULL DEFAULT 0,
                downloaded_files INTEGER NOT NULL DEFAULT 0,
                account_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                filename TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_sub2api_card_import_status_time
                ON sub2api_card_import_records(status, id DESC);
            """
        )


def _bounded_count(value: Any, name: str, *, maximum: int = 100000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if number < 0 or number > maximum:
        raise ValueError(f"{name} 超出允许范围")
    return number


def _details(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    account_ids = []
    for raw_id in value.get("new_account_ids", []) if isinstance(value.get("new_account_ids"), list) else []:
        try:
            account_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in account_ids:
            account_ids.append(account_id)
    missing_accounts = []
    raw_missing = value.get("missing_accounts")
    if isinstance(raw_missing, list):
        for item in raw_missing[:50]:
            if not isinstance(item, dict):
                continue
            missing_accounts.append({
                "name": str(item.get("name") or "")[:160],
                "platform": str(item.get("platform") or "")[:80],
                "type": str(item.get("type") or "")[:80],
                "count": _bounded_count(item.get("count", 1), "缺失账号数量", maximum=1000),
            })
    return {"new_account_ids": account_ids[:100], "missing_accounts": missing_accounts}


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _record(row: sqlite3.Row) -> dict[str, Any]:
    try:
        details = json.loads(_row_value(row, "details_json", "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        details = {}
    return {
        "id": int(row["id"]),
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "mode": row["mode"],
        "status": row["status"],
        "stage": row["stage"],
        **{field: int(_row_value(row, field, 0) or 0) for field in COUNT_FIELDS},
        "filename": row["filename"],
        "message": row["message"],
        "details": details if isinstance(details, dict) else {},
    }


def create_record(
    database_factory: DatabaseFactory,
    payload: dict[str, Any],
    *,
    now: Callable[[], str],
) -> dict[str, Any]:
    mode = str(payload.get("mode") or "manual").strip().lower()
    if mode not in {"manual", "auto"}:
        raise ValueError("卡密推送模式无效")
    card_count = _bounded_count(payload.get("card_count", 0), "卡密数量", maximum=100)
    if card_count < 1:
        raise ValueError("卡密数量应为 1 到 100 个")
    timestamp = now()
    with database_factory() as connection:
        cursor = connection.execute(
            """
            INSERT INTO sub2api_card_import_records(
                started_at, updated_at, mode, status, stage, card_count, message
            ) VALUES(?, ?, ?, 'running', 'verify', ?, ?)
            """,
            (timestamp, timestamp, mode, card_count, str(payload.get("message") or "")[:500]),
        )
        record_id = int(cursor.lastrowid)
        connection.execute(
            """
            DELETE FROM sub2api_card_import_records
            WHERE id NOT IN (
                SELECT id FROM sub2api_card_import_records ORDER BY id DESC LIMIT 500
            )
            """
        )
        row = connection.execute(
            "SELECT * FROM sub2api_card_import_records WHERE id = ?", (record_id,)
        ).fetchone()
    return _record(row)


def update_record(
    database_factory: DatabaseFactory,
    record_id: int,
    payload: dict[str, Any],
    *,
    now: Callable[[], str],
) -> dict[str, Any]:
    with database_factory() as connection:
        row = connection.execute(
            "SELECT * FROM sub2api_card_import_records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFound("卡密导入记录不存在")

        status = str(payload.get("status", row["status"])).strip().lower()
        stage = str(payload.get("stage", row["stage"])).strip().lower()
        if status not in STATUSES:
            raise ValueError("卡密导入状态无效")
        if stage not in STAGES:
            raise ValueError("卡密导入阶段无效")
        counts = {
            field: _bounded_count(
                payload.get(field, row[field]),
                field,
                maximum=100 if field in {"card_count", "verified_count"} else 100000,
            )
            for field in COUNT_FIELDS
        }
        timestamp = now()
        completed_at = timestamp if status in {"success", "failed"} else None
        details = _details(payload.get("details", json.loads(row["details_json"] or "{}")))
        connection.execute(
            """
            UPDATE sub2api_card_import_records SET
                updated_at = ?, completed_at = ?, status = ?, stage = ?,
                card_count = ?, verified_count = ?, downloaded_files = ?,
                account_count = ?, success_count = ?, failed_count = ?,
                filename = ?, message = ?, details_json = ?
            WHERE id = ?
            """,
            (
                timestamp,
                completed_at,
                status,
                stage,
                *(counts[field] for field in COUNT_FIELDS),
                str(payload.get("filename", row["filename"]) or "")[:240],
                str(payload.get("message", row["message"]) or "")[:500],
                json.dumps(details, ensure_ascii=False),
                record_id,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM sub2api_card_import_records WHERE id = ?", (record_id,)
        ).fetchone()
    return _record(updated)


def list_records(
    database_factory: DatabaseFactory,
    query_values: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    query_values = query_values or {}
    status = str(query_values.get("status", ["all"])[0] or "all").strip().lower()
    if status not in STATUSES | {"all"}:
        raise ValueError("卡密导入状态筛选无效")
    try:
        limit = max(1, min(100, int(query_values.get("limit", ["30"])[0])))
    except (TypeError, ValueError) as exc:
        raise ValueError("卡密导入记录数量无效") from exc
    if status == "all":
        where = ""
        parameters: tuple[Any, ...] = ()
    elif status == "pending":
        where = "WHERE status IN ('running', 'pending')"
        parameters = ()
    else:
        where = "WHERE status = ?"
        parameters = (status,)
    with database_factory() as connection:
        rows = connection.execute(
            f"SELECT * FROM sub2api_card_import_records {where} ORDER BY id DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        filtered_total = connection.execute(
            f"SELECT COUNT(*) FROM sub2api_card_import_records {where}", parameters
        ).fetchone()[0]
        summary = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status IN ('running', 'pending') THEN 1 ELSE 0 END) AS pending,
                SUM(success_count) AS successful_accounts,
                SUM(failed_count) AS failed_accounts
            FROM sub2api_card_import_records
            """
        ).fetchone()
    return {
        "items": [_record(row) for row in rows],
        "total": int(filtered_total or 0),
        "status": status,
        "summary": {
            key: int(summary[key] or 0)
            for key in ("total", "success", "failed", "pending", "successful_accounts", "failed_accounts")
        },
    }
