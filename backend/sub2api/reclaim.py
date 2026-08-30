"""401 detection and account-reclaim operations for Sub2API."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable

from .constants import MAX_RECLAIM_CODES


ERROR_401_TEXT_PATTERNS = (
    re.compile(r"(?i)\bhttp(?:/[0-9.]+)?\s*401\b"),
    re.compile(r"(?i)\b(?:oauth|status(?:_code|\s+code)?|code)\s*[:=()\[\]-]*\s*401\b"),
    re.compile(r"(?i)\bunauthorized\s*[:=()\[\]-]*\s*401\b"),
    re.compile(r"(?i)\b401\s*[:=()\[\]-]*\s*unauthorized\b"),
    re.compile(
        r"(?i)\b(?:token\s+revoked|invalid(?:ated)?\s+oauth\s+token)\b"
        r"[^\r\n]{0,80}\(\s*401\s*\)"
    ),
)


def error_text_is_401(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in ERROR_401_TEXT_PATTERNS)


def structured_error_is_401(value: Any, *, error_context: bool = False) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).strip().lower()
            child_context = error_context or any(
                token in normalized_key for token in ("error", "reason", "failure", "response")
            )
            if normalized_key in (
                "status_code",
                "http_status",
                "http_status_code",
                "upstream_status",
            ):
                try:
                    if int(child) == 401:
                        return True
                except (TypeError, ValueError):
                    pass
            if child_context and normalized_key in ("status", "code"):
                try:
                    if int(child) == 401:
                        return True
                except (TypeError, ValueError):
                    pass
            if child_context and error_text_is_401(child):
                return True
            if child_context and structured_error_is_401(child, error_context=True):
                return True
        return False
    if isinstance(value, list):
        return error_context and any(
            structured_error_is_401(item, error_context=True) for item in value
        )
    return error_context and error_text_is_401(value)


def account_is_401(account: dict[str, Any]) -> bool:
    for key in ("status_code", "http_status", "http_status_code", "upstream_status", "status"):
        try:
            if int(account.get(key)) == 401:
                return True
        except (TypeError, ValueError):
            pass
    for key in ("error_message", "temp_unschedulable_reason", "last_error", "error", "detail"):
        value = account.get(key)
        if error_text_is_401(value) or structured_error_is_401(value, error_context=True):
            return True
    return structured_error_is_401(account.get("extra"), error_context=False)


def download_payloads(
    reclaim_result: dict[str, Any],
    client: Any,
    *,
    exclude_order_nos: list[str] | None = None,
    max_payload_bytes: int,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    total_bytes = 0
    excluded = {str(value) for value in (exclude_order_nos or []) if str(value)}
    tasks = reclaim_result.get("all_tasks") if isinstance(reclaim_result, dict) else None
    if not isinstance(tasks, list):
        return payloads
    for task in tasks[:MAX_RECLAIM_CODES]:
        if not isinstance(task, dict) or task.get("status") != "done":
            continue
        order_no = str(task.get("order_no") or "").strip()
        token = str(task.get("download_token") or "").strip()
        if not order_no or not token or order_no in excluded:
            continue
        try:
            content = client.download(order_no, token)
        except Exception:
            continue
        if (
            not content
            or len(content) > max_payload_bytes
            or total_bytes + len(content) > 24 * 1024 * 1024
        ):
            continue
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict) or not isinstance(parsed.get("accounts"), list) or not parsed["accounts"]:
            continue
        total_bytes += len(content)
        payloads.append(
            {
                "task": task,
                "filename": f"{order_no}.json",
                "content_base64": base64.b64encode(content).decode("ascii"),
                "data": parsed,
            }
        )
    return payloads


def reclaim_401_accounts(
    *,
    config: dict[str, Any],
    accounts_loader: Callable[[dict[str, Any]], list[dict[str, Any]]],
    redeem_client_factory: Callable[[], Any],
    to_json: Callable[[Any], Any],
    max_payload_bytes: int,
    include_downloads: bool = True,
    exclude_order_nos: list[str] | None = None,
) -> dict[str, Any]:
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    accounts = accounts_loader(config)
    accounts_401 = [account for account in accounts if account_is_401(account)]

    from redeem_api_sdk import extract_card_code_from_name

    card_codes: list[str] = []
    missing: list[dict[str, Any]] = []
    seen: set[str] = set()
    for account in accounts_401:
        card_code = extract_card_code_from_name(str(account.get("name") or ""))
        if not card_code or len(card_code) < 8 or "-" not in card_code:
            missing.append({"id": account.get("id"), "name": str(account.get("name") or "")[:200]})
            continue
        if card_code not in seen:
            seen.add(card_code)
            card_codes.append(card_code)

    reclaim_result = None
    downloaded_payloads: list[dict[str, Any]] = []
    if card_codes:
        client = redeem_client_factory()
        reclaim_result = to_json(client.batch_reclaim(card_codes, mode="401"))
        if include_downloads and isinstance(reclaim_result, dict):
            downloaded_payloads = download_payloads(
                reclaim_result,
                client,
                exclude_order_nos=exclude_order_nos,
                max_payload_bytes=max_payload_bytes,
            )
    return {
        "ok": reclaim_result is None or bool(reclaim_result.get("ok", False)),
        "scanned_accounts": len(accounts),
        "accounts_401": len(accounts_401),
        "card_code_count": len(card_codes),
        "missing_card_code_count": len(missing),
        "skipped_non_401": len(accounts) - len(accounts_401),
        "submitted": bool(card_codes),
        "missing_card_code_accounts": missing[:50],
        "reclaim_card_codes": card_codes,
        "downloaded_payloads": downloaded_payloads,
        "result": reclaim_result,
    }


def refresh_reclaim(
    card_codes: list[str],
    *,
    redeem_client_factory: Callable[[], Any],
    to_json: Callable[[Any], Any],
    max_payload_bytes: int,
    exclude_order_nos: list[str] | None = None,
) -> dict[str, Any]:
    normalized = list(dict.fromkeys(str(code).strip() for code in card_codes if str(code).strip()))
    if not normalized or len(normalized) > MAX_RECLAIM_CODES:
        raise ValueError(f"卡密数量应为 1 到 {MAX_RECLAIM_CODES} 个")
    client = redeem_client_factory()
    result = to_json(client.refresh_progress(normalized))
    downloads = (
        download_payloads(
            result,
            client,
            exclude_order_nos=exclude_order_nos,
            max_payload_bytes=max_payload_bytes,
        )
        if isinstance(result, dict)
        else []
    )
    return {
        "ok": bool(isinstance(result, dict) and result.get("ok", False)),
        "reclaim_card_codes": normalized,
        "downloaded_payloads": downloads,
        "result": result,
    }
