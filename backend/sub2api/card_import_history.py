"""Persistent audit records for direct card verification and Sub2API pushes."""

from __future__ import annotations

import json
import re
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
RETRYABLE_STATUSES = {"pending", "failed"}
MAX_RETRY_CONTEXT_BYTES = 8 * 1024 * 1024


class RecordNotFound(LookupError):
    pass


class RetryNotAllowed(RuntimeError):
    pass


class RetryFailed(RuntimeError):
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
                details_json TEXT NOT NULL DEFAULT '{}',
                card_codes_json TEXT NOT NULL DEFAULT '[]',
                retry_context_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_sub2api_card_import_status_time
                ON sub2api_card_import_records(status, id DESC);
            """
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(sub2api_card_import_records)")
        }
        if "card_codes_json" not in columns:
            connection.execute(
                "ALTER TABLE sub2api_card_import_records "
                "ADD COLUMN card_codes_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "retry_context_json" not in columns:
            connection.execute(
                "ALTER TABLE sub2api_card_import_records "
                "ADD COLUMN retry_context_json TEXT NOT NULL DEFAULT '{}'"
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


def _card_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[\s,，]+", value)
    if not isinstance(value, list):
        raise ValueError("card_codes 必须是数组")
    codes: list[str] = []
    for raw_code in value:
        code = str(raw_code).strip()
        if not code or code in codes:
            continue
        if len(code) > 240:
            raise ValueError("卡密长度超出允许范围")
        codes.append(code)
    if len(codes) > 100:
        raise ValueError("一次最多保存 100 个卡密")
    return codes


def _retry_context(value: Any) -> dict[str, Any]:
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict) or "data" not in value:
        raise ValueError("重推上下文无效")
    source = value.get("data")
    if not isinstance(source, (dict, list)):
        raise ValueError("重推账号数据无效")

    proxy_id = value.get("proxy_id")
    if proxy_id not in (None, ""):
        try:
            proxy_id = int(proxy_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("重推代理编号无效") from exc
        if proxy_id < 1:
            raise ValueError("重推代理编号无效")
    else:
        proxy_id = None

    raw_group_ids = value.get("group_ids", [])
    if not isinstance(raw_group_ids, list):
        raise ValueError("重推分组编号必须是数组")
    group_ids: list[int] = []
    for raw_group_id in raw_group_ids[:100]:
        try:
            group_id = int(raw_group_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("重推分组编号无效") from exc
        if group_id > 0 and group_id not in group_ids:
            group_ids.append(group_id)

    fingerprint_mode = str(value.get("codex_fingerprint_mode") or "off").strip().lower()
    if fingerprint_mode not in {"off", "device", "session", "full"}:
        raise ValueError("重推指纹模式无效")
    endpoint = str(value.get("endpoint") or "/api/v1/admin/accounts/data").strip()
    if not endpoint.startswith("/") or len(endpoint) > 240:
        raise ValueError("重推接口路径无效")
    raw_order_nos = value.get("reclaim_order_nos", [])
    if not isinstance(raw_order_nos, list):
        raise ValueError("重推找回订单必须是数组")
    order_nos = list(dict.fromkeys(
        order_no
        for order_no in (str(item).strip() for item in raw_order_nos[:100])
        if re.fullmatch(r"[A-Za-z0-9_-]{3,160}", order_no)
    ))
    normalized = {
        "data": source,
        "assign_existing": bool(value.get("assign_existing", False)),
        "proxy_id": proxy_id,
        "group_ids": group_ids,
        "codex_fingerprint_mode": fingerprint_mode,
        "endpoint": endpoint,
        "reclaim_order_nos": order_nos,
    }
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_RETRY_CONTEXT_BYTES:
        raise ValueError("重推账号数据超过 8 MB 限制")
    return normalized


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
    try:
        card_codes = _card_codes(json.loads(_row_value(row, "card_codes_json", "[]") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        card_codes = []
    try:
        retry_context = json.loads(_row_value(row, "retry_context_json", "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        retry_context = {}
    status = row["status"]
    return {
        "id": int(row["id"]),
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "mode": row["mode"],
        "status": status,
        "stage": row["stage"],
        **{field: int(_row_value(row, field, 0) or 0) for field in COUNT_FIELDS},
        "filename": row["filename"],
        "message": row["message"],
        "details": details if isinstance(details, dict) else {},
        "card_codes": card_codes if isinstance(card_codes, list) else [],
        "retryable": status in RETRYABLE_STATUSES and isinstance(retry_context, dict) and bool(retry_context.get("data")),
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
    card_codes = _card_codes(payload.get("card_codes"))
    card_count = _bounded_count(payload.get("card_count", len(card_codes)), "卡密数量", maximum=100)
    if card_count < 1:
        raise ValueError("卡密数量应为 1 到 100 个")
    if card_codes and len(card_codes) != card_count:
        raise ValueError("卡密数量与卡密列表不一致")
    timestamp = now()
    with database_factory() as connection:
        cursor = connection.execute(
            """
            INSERT INTO sub2api_card_import_records(
                started_at, updated_at, mode, status, stage, card_count, message,
                card_codes_json
            ) VALUES(?, ?, ?, 'running', 'verify', ?, ?, ?)
            """,
            (
                timestamp,
                timestamp,
                mode,
                card_count,
                str(payload.get("message") or "")[:500],
                json.dumps(card_codes, ensure_ascii=False),
            ),
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
        try:
            stored_details = json.loads(row["details_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            stored_details = {}
        try:
            stored_retry_context = json.loads(
                _row_value(row, "retry_context_json", "{}") or "{}"
            )
        except (TypeError, json.JSONDecodeError):
            stored_retry_context = {}
        details = _details(payload.get("details", stored_details))
        retry_context = (
            _retry_context(payload.get("retry_context"))
            if "retry_context" in payload
            else stored_retry_context
        )
        connection.execute(
            """
            UPDATE sub2api_card_import_records SET
                updated_at = ?, completed_at = ?, status = ?, stage = ?,
                card_count = ?, verified_count = ?, downloaded_files = ?,
                account_count = ?, success_count = ?, failed_count = ?,
                filename = ?, message = ?, details_json = ?, retry_context_json = ?
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
                json.dumps(retry_context, ensure_ascii=False, separators=(",", ":")),
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
        page = max(1, int(query_values.get("page", ["1"])[0]))
        page_size_value = query_values.get("page_size", query_values.get("limit", ["10"]))[0]
        page_size = max(1, min(50, int(page_size_value)))
    except (TypeError, ValueError) as exc:
        raise ValueError("卡密导入记录分页参数无效") from exc
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
        filtered_total = connection.execute(
            f"SELECT COUNT(*) FROM sub2api_card_import_records {where}", parameters
        ).fetchone()[0]
        pages = max(1, (int(filtered_total or 0) + page_size - 1) // page_size)
        page = min(page, pages)
        rows = connection.execute(
            f"SELECT * FROM sub2api_card_import_records {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()
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
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "status": status,
        "summary": {
            key: int(summary[key] or 0)
            for key in ("total", "success", "failed", "pending", "successful_accounts", "failed_accounts")
        },
    }


def retry_record(
    database_factory: DatabaseFactory,
    record_id: int,
    *,
    import_payload: Callable[..., dict[str, Any]],
    now: Callable[[], str],
) -> dict[str, Any]:
    timestamp = now()
    with database_factory() as connection:
        row = connection.execute(
            "SELECT * FROM sub2api_card_import_records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFound("卡密导入记录不存在")
        if row["status"] not in RETRYABLE_STATUSES:
            raise RetryNotAllowed("仅推送失败或等待推送的记录可以重新推送")
        try:
            context = _retry_context(
                json.loads(_row_value(row, "retry_context_json", "{}") or "{}")
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RetryNotAllowed("该记录的重推数据已损坏") from exc
        if not context:
            raise RetryNotAllowed("该记录尚未生成可重新推送的账号数据")
        updated = connection.execute(
            """
            UPDATE sub2api_card_import_records
            SET status = 'running', stage = 'push', completed_at = NULL,
                updated_at = ?, message = '正在重新推送账号文件'
            WHERE id = ? AND status IN ('pending', 'failed')
            """,
            (timestamp, record_id),
        )
        if updated.rowcount != 1:
            raise RetryNotAllowed("该记录正在处理，请稍后刷新")

    try:
        result = import_payload(
            context["data"],
            proxy_id=context["proxy_id"],
            group_ids=context["group_ids"],
            codex_fingerprint_mode=context["codex_fingerprint_mode"],
            assign_existing=context["assign_existing"],
            endpoint=context["endpoint"],
            reclaim_order_nos=context["reclaim_order_nos"],
        )
    except Exception as exc:
        update_record(
            database_factory,
            record_id,
            {
                "status": "failed",
                "stage": "error",
                "message": f"重新推送失败：{str(exc)[:440]}",
            },
            now=now,
        )
        raise RetryFailed(str(exc)[:440]) from exc

    verification = result.get("import_verification") if isinstance(result, dict) else None
    verification = verification if isinstance(verification, dict) else {}
    def result_count(name: str) -> int:
        try:
            return max(0, min(100000, int(verification.get(name) or 0)))
        except (TypeError, ValueError):
            return 0

    expected = result_count("expected")
    matched = result_count("matched")
    failed_count = max(result_count("failed"), expected - matched)
    confirmed = bool(verification.get("confirmed"))
    record = update_record(
        database_factory,
        record_id,
        {
            "status": "success" if confirmed else "failed",
            "stage": "done" if confirmed else "error",
            "account_count": expected,
            "success_count": matched,
            "failed_count": failed_count,
            "message": (
                f"重新推送并核验成功，共 {matched} 个账号"
                if confirmed
                else f"重新推送后仅核验 {matched}/{expected} 个账号"
            ),
            "details": {
                "new_account_ids": verification.get("new_account_ids", []),
                "missing_accounts": verification.get("missing", []),
            },
        },
        now=now,
    )
    return {"ok": confirmed, "record": record, "result": result}
