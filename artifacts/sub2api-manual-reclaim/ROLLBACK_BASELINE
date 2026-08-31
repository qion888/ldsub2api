"""Pure validation and transformation helpers for Sub2API payloads."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from .constants import (
    CODEX_FINGERPRINT_MODES,
    MAX_ACCOUNTS,
    MAX_BUNDLES,
    MAX_GROUPS,
    MAX_PROXIES,
)


def validate_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("账号 JSON 必须是对象")
    if data.get("type") not in (None, "sub2api-data", "sub2api-bundle"):
        raise ValueError("账号 JSON type 必须是 sub2api-data 或 sub2api-bundle")
    if data.get("version") not in (None, 1):
        raise ValueError("账号 JSON version 必须为 1")
    accounts = data.get("accounts")
    proxies = data.get("proxies", [])
    if not isinstance(accounts, list) or not accounts or len(accounts) > MAX_ACCOUNTS:
        raise ValueError(f"账号 JSON 至少包含 1 个 accounts，最多 {MAX_ACCOUNTS} 个")
    if not isinstance(proxies, list) or len(proxies) > MAX_PROXIES:
        raise ValueError("账号 JSON proxies 格式无效")
    normalized_accounts = []
    for index, account in enumerate(accounts):
        if not isinstance(account, dict) or not isinstance(account.get("credentials"), dict) or not account.get("credentials"):
            raise ValueError(f"第 {index + 1} 个账号缺少 credentials")
        normalized_account = dict(account)
        normalized_account["name"] = str(account.get("name") or f"导入账号 {index + 1}")[:200]
        normalized_accounts.append(normalized_account)
    normalized = dict(data)
    normalized["type"] = data.get("type") or "sub2api-data"
    normalized["version"] = 1
    normalized["proxies"] = [dict(proxy) if isinstance(proxy, dict) else proxy for proxy in proxies]
    normalized["accounts"] = normalized_accounts
    return normalized


def merge_data(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return validate_data(data)
    if not isinstance(data, list) or not data or len(data) > MAX_BUNDLES:
        raise ValueError(f"账号 JSON 必须是对象，或 1 到 {MAX_BUNDLES} 个对象组成的数组")
    accounts: list[dict[str, Any]] = []
    proxies: list[dict[str, Any]] = []
    for index, source in enumerate(data):
        try:
            normalized = validate_data(source)
        except ValueError as exc:
            raise ValueError(f"第 {index + 1} 个 JSON：{exc}") from exc
        accounts.extend(normalized["accounts"])
        proxies.extend(normalized["proxies"])
        if len(accounts) > MAX_ACCOUNTS or len(proxies) > MAX_PROXIES:
            raise ValueError(f"合并后的账号或代理数量不能超过 {MAX_ACCOUNTS} 个")
    return {
        "type": "sub2api-data",
        "version": 1,
        "accounts": accounts,
        "proxies": proxies,
    }


def apply_codex_fingerprint_mode(
    normalized: dict[str, Any], raw_mode: Any
) -> tuple[dict[str, Any], str | None, int]:
    if raw_mode is None:
        return normalized, None, 0
    if not isinstance(raw_mode, str):
        raise ValueError("Codex 指纹收敛模式无效")
    mode = raw_mode.strip().lower()
    if mode not in CODEX_FINGERPRINT_MODES:
        raise ValueError("Codex 指纹收敛模式必须是 off、device、session 或 full")

    result = dict(normalized)
    accounts = []
    codex_account_count = 0
    for source in normalized["accounts"]:
        account = deepcopy(source)
        if (
            str(account.get("platform") or "").strip().lower() == "openai"
            and str(account.get("type") or "").strip().lower() in ("oauth", "setup-token")
        ):
            extra = dict(account.get("extra")) if isinstance(account.get("extra"), dict) else {}
            if mode == "off":
                extra.pop("codex_fingerprint_mode", None)
            else:
                extra["codex_fingerprint_mode"] = mode
            account["extra"] = extra
            codex_account_count += 1
        accounts.append(account)
    result["accounts"] = accounts
    return result, mode, codex_account_count


def payload_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def payload_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    value = payload_data(payload)
    if isinstance(value, dict):
        for key in ("items", "list", *keys):
            candidate = value.get(key)
            if isinstance(candidate, list):
                value = candidate
                break
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def upstream_error(status: int, payload: Any, raw: str) -> str:
    if isinstance(payload, dict):
        detail = payload.get("message") or payload.get("error") or payload.get("detail")
        if detail:
            return str(detail)[:500]
    return raw[:500] or f"HTTP {status}"


def created_account_ids(payload: Any) -> set[int]:
    result = payload_data(payload)
    rows = result.get("results") if isinstance(result, dict) else None
    ids: set[int] = set()
    if not isinstance(rows, list):
        return ids
    for row in rows:
        if not isinstance(row, dict) or row.get("success") is False:
            continue
        try:
            account_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if account_id > 0:
            ids.add(account_id)
    return ids


def fingerprint_targets(
    imported_accounts: list[dict[str, Any]],
    upstream_accounts: list[dict[str, Any]],
    created_ids: set[int],
) -> list[dict[str, Any]]:
    expected: dict[tuple[str, str, str], int] = {}
    for account in imported_accounts:
        platform = str(account.get("platform") or "").strip().lower()
        account_type = str(account.get("type") or "").strip().lower()
        if platform != "openai" or account_type not in ("oauth", "setup-token"):
            continue
        key = (str(account.get("name") or ""), platform, account_type)
        expected[key] = expected.get(key, 0) + 1

    candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for account in upstream_accounts:
        try:
            account_id = int(account.get("id"))
        except (TypeError, ValueError):
            continue
        if created_ids and account_id not in created_ids:
            continue
        key = (
            str(account.get("name") or ""),
            str(account.get("platform") or "").strip().lower(),
            str(account.get("type") or "").strip().lower(),
        )
        if key in expected:
            candidates.setdefault(key, []).append(account)

    targets = []
    for key, count in expected.items():
        rows = sorted(candidates.get(key, []), key=lambda item: int(item.get("id") or 0), reverse=True)
        targets.extend(rows[:count])
    return targets


def fingerprint_verification_error(mode: str | None, eligible: int, error: Exception) -> dict[str, Any]:
    return {
        "mode": mode,
        "eligible": eligible,
        "matched": 0,
        "verified": 0,
        "repaired": 0,
        "unresolved": eligible,
        "error": str(error)[:500],
    }


def datetime_value(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(value), timezone.utc)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def monitor_summary(
    accounts: list[dict[str, Any]],
    proxies: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    now_iso: Callable[[], str] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    expiring_cutoff = current.timestamp() + (7 * 86400)
    platform_counts: dict[str, dict[str, int]] = {}
    recent_errors = []
    active_accounts = 0
    error_accounts = 0
    schedulable_accounts = 0
    rate_limited_accounts = 0
    expiring_accounts = 0

    for account in accounts:
        status = str(account.get("status") or "unknown").strip().lower()
        platform = str(account.get("platform") or "unknown").strip().lower() or "unknown"
        bucket = platform_counts.setdefault(platform, {"count": 0, "errors": 0})
        bucket["count"] += 1
        if status == "active":
            active_accounts += 1
        if bool(account.get("schedulable", status == "active")):
            schedulable_accounts += 1
        error_message = str(account.get("error_message") or "").strip()
        if status == "error" or error_message:
            error_accounts += 1
            bucket["errors"] += 1
            recent_errors.append({
                "id": account.get("id"),
                "name": str(account.get("name") or f"账号 {account.get('id') or '-'}")[:160],
                "platform": platform,
                "status": status,
                "error": error_message[:300] or "账号状态异常",
                "updated_at": account.get("updated_at"),
            })
        account_expiry = datetime_value(account.get("expires_at"))
        if account_expiry and current.timestamp() <= account_expiry.timestamp() <= expiring_cutoff:
            expiring_accounts += 1
        limited_until = max(
            (
                stamp.timestamp()
                for stamp in (
                    datetime_value(account.get("rate_limit_reset_at")),
                    datetime_value(account.get("overload_until")),
                    datetime_value(account.get("temp_unschedulable_until")),
                )
                if stamp is not None
            ),
            default=0,
        )
        if limited_until > current.timestamp():
            rate_limited_accounts += 1

    def error_sort_key(item: dict[str, Any]) -> float:
        stamp = datetime_value(item.get("updated_at"))
        return stamp.timestamp() if stamp else 0

    recent_errors.sort(key=error_sort_key, reverse=True)
    active_proxies = sum(1 for proxy in proxies if str(proxy.get("status") or "").lower() == "active")
    unhealthy_proxies = sum(
        1
        for proxy in proxies
        if str(proxy.get("status") or "").lower() not in ("", "active")
        or str(proxy.get("latency_status") or "").lower() in {"error", "failed", "timeout", "unreachable"}
    )
    expiring_proxies = sum(
        1
        for proxy in proxies
        if (expiry := datetime_value(proxy.get("expires_at")))
        and current.timestamp() <= expiry.timestamp() <= expiring_cutoff
    )
    inactive_groups = sum(1 for group in groups if str(group.get("status") or "").lower() not in ("", "active"))
    platforms = [
        {"platform": platform, **counts}
        for platform, counts in sorted(platform_counts.items(), key=lambda item: (-item[1]["count"], item[0]))
    ]
    fetched_at = now_iso() if now_iso else current.isoformat(timespec="seconds")
    return {
        "fetched_at": fetched_at,
        "total_accounts": len(accounts),
        "active_accounts": active_accounts,
        "error_accounts": error_accounts,
        "inactive_accounts": max(0, len(accounts) - active_accounts - error_accounts),
        "schedulable_accounts": schedulable_accounts,
        "unschedulable_accounts": max(0, len(accounts) - schedulable_accounts),
        "rate_limited_accounts": rate_limited_accounts,
        "expiring_accounts": expiring_accounts,
        "active_proxies": active_proxies,
        "unhealthy_proxies": unhealthy_proxies,
        "expiring_proxies": expiring_proxies,
        "inactive_groups": inactive_groups,
        "platforms": platforms,
        "recent_errors": recent_errors[:8],
    }


def assignment_payload(
    normalized: dict[str, Any], proxy_id: Any, raw_group_ids: Any
) -> tuple[dict[str, Any], int | None, list[int]]:
    selected_proxy_id: int | None = None
    if proxy_id not in (None, ""):
        try:
            selected_proxy_id = int(proxy_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Sub2API 代理编号无效") from exc
        if selected_proxy_id < 1:
            raise ValueError("Sub2API 代理编号无效")

    if raw_group_ids is None:
        raw_group_ids = []
    if not isinstance(raw_group_ids, list) or len(raw_group_ids) > MAX_GROUPS:
        raise ValueError(f"Sub2API 分组必须是最多 {MAX_GROUPS} 项的数组")
    try:
        group_ids = sorted({int(value) for value in raw_group_ids})
    except (TypeError, ValueError) as exc:
        raise ValueError("Sub2API 分组编号无效") from exc
    if any(value < 1 for value in group_ids):
        raise ValueError("Sub2API 分组编号无效")

    accounts = []
    for index, source in enumerate(normalized["accounts"]):
        platform = str(source.get("platform") or "").strip()
        account_type = str(source.get("type") or "").strip()
        if not platform or not account_type:
            raise ValueError(f"第 {index + 1} 个账号缺少 platform 或 type")
        try:
            concurrency = int(source.get("concurrency") or 0)
            priority = int(source.get("priority") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {index + 1} 个账号 concurrency 或 priority 无效") from exc
        item: dict[str, Any] = {
            "name": str(source.get("name") or f"导入账号 {index + 1}")[:200],
            "platform": platform,
            "type": account_type,
            "credentials": source["credentials"],
            "extra": source.get("extra") if isinstance(source.get("extra"), dict) else {},
            "proxy_id": selected_proxy_id,
            "concurrency": concurrency,
            "priority": priority,
            "group_ids": group_ids,
        }
        for key in ("notes", "rate_multiplier", "expires_at", "auto_pause_on_expired"):
            if key in source:
                item[key] = source[key]
        accounts.append(item)
    return {"accounts": accounts}, selected_proxy_id, group_ids
