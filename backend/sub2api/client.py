"""HTTP-facing Sub2API client operations."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable
from urllib.parse import urlencode

from .payloads import (
    apply_codex_fingerprint_mode,
    assignment_payload,
    created_account_ids,
    fingerprint_targets,
    fingerprint_verification_error,
    merge_data,
    payload_data,
    payload_list,
    upstream_error,
)

JsonRequest = Callable[..., tuple[int, Any, str]]


def fetch_accounts(
    config: dict[str, Any],
    *,
    request_json: JsonRequest,
    platform: str = "",
    account_type: str = "",
) -> list[dict[str, Any]]:
    headers = {"x-api-key": config["admin_key"]}
    accounts: list[dict[str, Any]] = []
    page = 1
    while page <= 200:
        params: dict[str, Any] = {
            "page": page,
            "page_size": 100,
            "sort_by": "created_at",
            "sort_order": "desc",
        }
        if platform:
            params["platform"] = platform
        if account_type:
            params["type"] = account_type
        status, payload, raw = request_json(
            "GET",
            config["base_url"] + "/api/v1/admin/accounts?" + urlencode(params),
            headers=headers,
            timeout=30,
        )
        if not 200 <= status < 300:
            detail = upstream_error(status, payload, raw)
            raise RuntimeError(f"Sub2API accounts 返回 HTTP {status}: {detail}")
        value = payload_data(payload)
        items = payload_list(payload, "accounts")
        accounts.extend(items)
        if not isinstance(value, dict):
            break
        try:
            pages_value = value.get("pages")
            if pages_value is not None:
                pages = max(1, int(pages_value))
            else:
                total = max(0, int(value.get("total") or 0))
                size = max(1, int(value.get("page_size") or params["page_size"]))
                pages = max(1, (total + size - 1) // size)
        except (TypeError, ValueError):
            pages = 1
        if page >= pages or not items:
            break
        page += 1
    if page > 200:
        raise RuntimeError("Sub2API 账号分页超过 200 页，请先缩小账号范围")
    return accounts


def codex_accounts(
    config: dict[str, Any],
    *,
    accounts_loader: Callable[..., list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    result = []
    for account_type in ("oauth", "setup-token"):
        result.extend(accounts_loader(config, platform="openai", account_type=account_type))
    return result


def _account_identity(account: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(account.get("name") or "").strip(),
        str(account.get("platform") or "").strip().lower(),
        str(account.get("type") or "").strip().lower(),
    )


def _positive_account_id(account: dict[str, Any]) -> int | None:
    try:
        account_id = int(account.get("id"))
    except (TypeError, ValueError):
        return None
    return account_id if account_id > 0 else None


def _import_counts(payload: Any, expected: int) -> tuple[int, int]:
    value = payload_data(payload)
    if not isinstance(value, dict):
        return expected, 0
    accepted_value = value.get("success", value.get("account_created", expected))
    failed_value = value.get("failed", value.get("account_failed", 0))
    try:
        accepted = max(0, min(expected, int(accepted_value)))
    except (TypeError, ValueError):
        accepted = expected
    try:
        failed = max(0, int(failed_value))
    except (TypeError, ValueError):
        failed = 0
    return accepted, failed


def verify_imported_accounts(
    expected_accounts: list[dict[str, Any]],
    before_accounts: list[dict[str, Any]],
    after_accounts: list[dict[str, Any]],
    import_payload: Any,
) -> dict[str, Any]:
    before_ids = {
        account_id
        for account in before_accounts
        if (account_id := _positive_account_id(account)) is not None
    }
    new_accounts = [
        account
        for account in after_accounts
        if (account_id := _positive_account_id(account)) is not None and account_id not in before_ids
    ]
    expected_counts = Counter(_account_identity(account) for account in expected_accounts)
    observed_counts = Counter(_account_identity(account) for account in new_accounts)
    matched = sum(min(count, observed_counts.get(identity, 0)) for identity, count in expected_counts.items())
    missing = []
    for identity, count in expected_counts.items():
        remaining = max(0, count - observed_counts.get(identity, 0))
        if remaining:
            missing.append({
                "name": identity[0],
                "platform": identity[1],
                "type": identity[2],
                "count": remaining,
            })
    accepted, failed = _import_counts(import_payload, len(expected_accounts))
    confirmed = accepted == len(expected_accounts) and failed == 0 and matched == len(expected_accounts)
    return {
        "confirmed": confirmed,
        "expected": len(expected_accounts),
        "accepted": accepted,
        "failed": failed,
        "observed_new": len(new_accounts),
        "matched": matched,
        "missing": missing[:50],
        "new_account_ids": [
            account_id
            for account in new_accounts
            if (account_id := _positive_account_id(account)) is not None
        ],
    }


ACCOUNT_PUBLIC_FIELDS = (
    "id", "name", "notes", "platform", "type", "proxy_id", "concurrency",
    "current_concurrency", "priority", "rate_multiplier", "status", "error_message",
    "schedulable", "expires_at", "created_at", "updated_at", "last_used_at",
    "rate_limited_at", "rate_limit_reset_at", "overload_until", "temp_unschedulable_until",
    "temp_unschedulable_reason", "quota_limit", "quota_used", "quota_daily_limit",
    "quota_daily_used", "quota_weekly_limit", "quota_weekly_used", "group_ids",
    "quota_daily_reset_mode", "quota_daily_reset_at", "quota_weekly_reset_mode",
    "quota_weekly_reset_at", "quota_reset_timezone", "session_window_start",
    "session_window_end", "session_window_status", "window_cost_limit", "base_rpm",
    "five_hour", "seven_day",
)

USAGE_WINDOW_FIELDS = (
    "utilization", "resets_at", "remaining_seconds", "used_requests", "limit_requests",
)
USAGE_WINDOW_STAT_FIELDS = ("requests", "tokens", "cost", "standard_cost", "user_cost")
USAGE_FIELDS = (
    "source", "updated_at", "five_hour", "seven_day", "seven_day_sonnet",
    "seven_day_fable", "thirty_day", "gemini_shared_daily", "gemini_pro_daily",
    "gemini_flash_daily", "gemini_shared_minute", "gemini_pro_minute",
    "gemini_flash_minute", "error_code", "error",
)


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    result = {key: account.get(key) for key in ACCOUNT_PUBLIC_FIELDS if key in account}
    for key in ("five_hour", "seven_day"):
        if key not in result:
            continue
        window = _public_usage_window(result[key])
        if window is None:
            result.pop(key)
        else:
            result[key] = window
    groups = account.get("groups")
    if isinstance(groups, list):
        result["groups"] = [
            {"id": group.get("id"), "name": str(group.get("name") or "")[:160]}
            for group in groups
            if isinstance(group, dict)
        ]
    proxy = account.get("proxy")
    if isinstance(proxy, dict):
        result["proxy"] = {
            key: proxy.get(key)
            for key in ("id", "name", "protocol", "host", "port", "status")
            if key in proxy
        }
    return result


def _public_usage_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    result = {key: window.get(key) for key in USAGE_WINDOW_FIELDS if key in window}
    stats = window.get("window_stats")
    if isinstance(stats, dict):
        result["window_stats"] = {
            key: stats.get(key)
            for key in USAGE_WINDOW_STAT_FIELDS
            if key in stats
        }
    return result


def _public_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    result: dict[str, Any] = {}
    for key in USAGE_FIELDS:
        if key not in usage:
            continue
        if key in {"source", "updated_at", "error_code", "error"}:
            value = usage.get(key)
            result[key] = str(value)[:500] if value is not None else None
            continue
        window = _public_usage_window(usage.get(key))
        if window is not None:
            result[key] = window
    return result


def fetch_account_page(
    config: dict[str, Any],
    *,
    request_json: JsonRequest,
    page: int = 1,
    page_size: int = 12,
    search: str = "",
    status_filter: str = "",
    platform: str = "",
) -> dict[str, Any]:
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    page = min(max(int(page), 1), 10000)
    page_size = min(max(int(page_size), 1), 100)
    params: dict[str, Any] = {
        "page": page,
        "page_size": page_size,
        "sort_by": "created_at",
        "sort_order": "desc",
    }
    search = str(search or "").strip()[:100]
    if search:
        params["search"] = search
    status_filter = str(status_filter or "").strip().lower()
    if status_filter in {"active", "inactive", "error"}:
        params["status"] = status_filter
    platform = str(platform or "").strip().lower()[:40]
    if platform:
        params["platform"] = platform

    headers = {"x-api-key": config["admin_key"]}
    status, payload, raw = request_json(
        "GET",
        config["base_url"] + "/api/v1/admin/accounts?" + urlencode(params),
        headers=headers,
        timeout=30,
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"Sub2API accounts 返回 HTTP {status}: {upstream_error(status, payload, raw)}")
    value = payload_data(payload)
    items = payload_list(payload, "accounts")
    public_items = [_public_account(account) for account in items]
    try:
        total = max(0, int(value.get("total") if isinstance(value, dict) else len(items)))
    except (TypeError, ValueError):
        total = len(items)

    usage: dict[str, Any] = {}
    usage_errors: dict[str, str] = {}
    account_ids = [
        account_id
        for account in public_items
        if (account_id := _positive_account_id(account)) is not None
    ]
    if account_ids:
        usage_status, usage_payload, usage_raw = request_json(
            "POST",
            config["base_url"] + "/api/v1/admin/accounts/usage/batch",
            {"account_ids": account_ids, "force": False},
            headers=headers,
            timeout=45,
        )
        if 200 <= usage_status < 300:
            usage_value = payload_data(usage_payload)
            if isinstance(usage_value, dict):
                raw_usage = usage_value.get("usage")
                raw_errors = usage_value.get("errors")
                allowed_ids = {str(account_id) for account_id in account_ids}
                usage = (
                    {
                        str(key): _public_usage(value)
                        for key, value in raw_usage.items()
                        if str(key) in allowed_ids and isinstance(value, dict)
                    }
                    if isinstance(raw_usage, dict)
                    else {}
                )
                usage_errors = (
                    {str(key): str(value)[:300] for key, value in raw_errors.items()}
                    if isinstance(raw_errors, dict)
                    else {}
                )
        else:
            usage_errors["_"] = upstream_error(usage_status, usage_payload, usage_raw)
    return {
        "ok": True,
        "items": public_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "usage": usage,
        "usage_errors": usage_errors,
    }


def _sse_test_result(payload: Any, raw: str) -> dict[str, Any]:
    candidates = []
    value = payload_data(payload)
    if isinstance(value, dict):
        candidates.append(value)
    for line in raw.splitlines():
        if not line.lstrip().startswith("data:"):
            continue
        try:
            candidate = json.loads(line.split(":", 1)[1].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            candidates.append(candidate)
    for candidate in reversed(candidates):
        if "success" in candidate or candidate.get("type") in {"result", "error"}:
            return candidate
    return candidates[-1] if candidates else {}


def test_account(config: dict[str, Any], account_id: int, *, request_json: JsonRequest) -> dict[str, Any]:
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    if account_id < 1:
        raise ValueError("Sub2API 账号编号无效")
    status, payload, raw = request_json(
        "POST",
        f"{config['base_url']}/api/v1/admin/accounts/{account_id}/test",
        {},
        headers={"x-api-key": config["admin_key"]},
        timeout=90,
    )
    result = _sse_test_result(payload, raw)
    ok = 200 <= status < 300 and result.get("success") is not False
    message = result.get("message") or result.get("error") or ("账号连接测试通过" if ok else f"HTTP {status}")
    return {
        "ok": ok,
        "account_id": account_id,
        "upstream_status": status,
        "message": str(message)[:500],
        "latency_ms": result.get("latency_ms"),
    }


def delete_account(config: dict[str, Any], account_id: int, *, request_json: JsonRequest) -> dict[str, Any]:
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    if account_id < 1:
        raise ValueError("Sub2API 账号编号无效")
    status, payload, raw = request_json(
        "DELETE",
        f"{config['base_url']}/api/v1/admin/accounts/{account_id}",
        headers={"x-api-key": config["admin_key"]},
        timeout=30,
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"Sub2API 删除账号返回 HTTP {status}: {upstream_error(status, payload, raw)}")
    return {"ok": True, "account_id": account_id, "result": payload_data(payload)}


def reconcile_codex_fingerprint(
    config: dict[str, Any],
    normalized: dict[str, Any],
    mode: str | None,
    import_payload: Any,
    *,
    request_json: JsonRequest,
    codex_accounts_loader: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> dict[str, Any]:
    eligible = [
        account for account in normalized["accounts"]
        if str(account.get("platform") or "").strip().lower() == "openai"
        and str(account.get("type") or "").strip().lower() in ("oauth", "setup-token")
    ]
    summary = {
        "mode": mode,
        "eligible": len(eligible),
        "matched": 0,
        "verified": 0,
        "repaired": 0,
        "unresolved": len(eligible),
    }
    if mode is None or not eligible:
        return summary

    upstream_accounts = codex_accounts_loader(config)
    created_ids = created_account_ids(import_payload)
    targets = fingerprint_targets(eligible, upstream_accounts, created_ids)
    summary["matched"] = len(targets)
    expected_values = {None, "", "off"} if mode == "off" else {mode}
    mismatched_ids = []
    for account in targets:
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        actual = str(extra.get("codex_fingerprint_mode") or "").strip().lower() or None
        if actual in expected_values:
            summary["verified"] += 1
        else:
            try:
                mismatched_ids.append(int(account["id"]))
            except (KeyError, TypeError, ValueError):
                continue

    if mismatched_ids:
        status, payload, raw = request_json(
            "POST",
            config["base_url"] + "/api/v1/admin/accounts/bulk-update",
            {"account_ids": mismatched_ids, "extra": {"codex_fingerprint_mode": mode}},
            headers={"x-api-key": config["admin_key"]},
            timeout=60,
        )
        if not 200 <= status < 300:
            detail = upstream_error(status, payload, raw)
            raise RuntimeError(f"Sub2API 指纹补写返回 HTTP {status}: {detail}")
        result = payload_data(payload)
        success_ids = result.get("success_ids") if isinstance(result, dict) else None
        submitted_ids = {
            int(account_id) for account_id in (success_ids if isinstance(success_ids, list) else mismatched_ids)
            if str(account_id).isdigit() and int(account_id) > 0
        }
        refreshed = codex_accounts_loader(config)
        for account in refreshed:
            try:
                account_id = int(account.get("id"))
            except (TypeError, ValueError):
                continue
            if account_id not in submitted_ids:
                continue
            extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
            actual = str(extra.get("codex_fingerprint_mode") or "").strip().lower() or None
            if actual in expected_values:
                summary["repaired"] += 1
        summary["verified"] += summary["repaired"]

    summary["unresolved"] = max(0, summary["eligible"] - summary["verified"])
    return summary


def fetch_options(
    config: dict[str, Any],
    *,
    request_json: JsonRequest,
    accounts_loader: Callable[[dict[str, Any]], list[dict[str, Any]]],
    monitor_builder: Callable[[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]],
) -> dict[str, Any]:
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    headers = {"x-api-key": config["admin_key"]}
    endpoints = {
        "proxies": "/api/v1/admin/proxies/all?with_count=true",
        "groups": "/api/v1/admin/groups/all",
    }
    responses: dict[str, Any] = {}
    for key, endpoint in endpoints.items():
        status, payload, raw = request_json(
            "GET", config["base_url"] + endpoint, headers=headers, timeout=20
        )
        if not 200 <= status < 300:
            detail = upstream_error(status, payload, raw)
            raise RuntimeError(f"Sub2API {key} 返回 HTTP {status}: {detail}")
        responses[key] = payload

    proxies = []
    for item in payload_list(responses["proxies"], "proxies"):
        try:
            proxy_id = int(item.get("id"))
            port = int(item.get("port") or 0)
            account_count = int(item.get("account_count") or 0)
        except (TypeError, ValueError):
            continue
        if proxy_id < 1:
            continue
        proxies.append({
            "id": proxy_id,
            "name": str(item.get("name") or f"代理 {proxy_id}")[:160],
            "protocol": str(item.get("protocol") or ""),
            "host": str(item.get("host") or "")[:255],
            "port": port,
            "status": str(item.get("status") or ""),
            "account_count": account_count,
            "expires_at": item.get("expires_at"),
            "fallback_mode": str(item.get("fallback_mode") or "none"),
            "latency_ms": item.get("latency_ms"),
            "latency_status": str(item.get("latency_status") or ""),
            "quality_grade": str(item.get("quality_grade") or ""),
            "quality_score": item.get("quality_score"),
            "country_code": str(item.get("country_code") or ""),
            "region": str(item.get("region") or "")[:120],
        })

    groups = []
    for item in payload_list(responses["groups"], "groups"):
        try:
            group_id = int(item.get("id"))
            account_count = int(item.get("account_count") or 0)
        except (TypeError, ValueError):
            continue
        if group_id < 1:
            continue
        groups.append({
            "id": group_id,
            "name": str(item.get("name") or f"分组 {group_id}")[:160],
            "platform": str(item.get("platform") or ""),
            "status": str(item.get("status") or ""),
            "account_count": account_count,
            "subscription_type": str(item.get("subscription_type") or ""),
            "rate_multiplier": item.get("rate_multiplier"),
        })

    accounts = accounts_loader(config)
    return {
        "ok": True,
        "proxy_service_available": True,
        "proxy_count": len(proxies),
        "group_count": len(groups),
        "proxies": proxies,
        "groups": groups,
        "monitor": monitor_builder(accounts, proxies, groups),
    }


def import_payload(
    source: Any,
    *,
    config: dict[str, Any],
    request_json: JsonRequest,
    fingerprint_reconciler: Callable[[dict[str, Any], dict[str, Any], str | None, Any], dict[str, Any]],
    accounts_loader: Callable[[dict[str, Any]], list[dict[str, Any]]],
    proxy_id: Any = None,
    group_ids: Any = None,
    codex_fingerprint_mode: Any = None,
    assign_existing: bool | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    normalized = merge_data(source)
    normalized, fingerprint_mode, codex_account_count = apply_codex_fingerprint_mode(
        normalized, codex_fingerprint_mode
    )
    before_accounts = accounts_loader(config)
    if assign_existing is None:
        assign_existing = proxy_id not in (None, "") or bool(group_ids)
    if assign_existing:
        request_payload, selected_proxy_id, selected_group_ids = assignment_payload(
            normalized, proxy_id, group_ids
        )
        target_endpoint = config["base_url"] + "/api/v1/admin/accounts/batch"
        mode = "assigned"
    else:
        selected_proxy_id, selected_group_ids = None, []
        import_endpoint = endpoint or "/api/v1/admin/accounts/data"
        if import_endpoint == "/api/v1/admin/accounts/data":
            request_payload = {"data": normalized, "skip_default_group_bind": True}
        elif import_endpoint == "/api/v1/admin/accounts/import/codex-session":
            request_payload = {"content": json.dumps(normalized, ensure_ascii=False), "update_existing": True}
        else:
            raise ValueError("Sub2API 导入接口无效")
        target_endpoint = config["base_url"] + import_endpoint
        mode = "data"
    status, payload, raw = request_json(
        "POST",
        target_endpoint,
        request_payload,
        headers={"x-api-key": config["admin_key"]},
        timeout=60,
    )
    if not 200 <= status < 300:
        detail = upstream_error(status, payload, raw)
        raise RuntimeError(f"Sub2API 返回 HTTP {status}: {detail}")
    try:
        fingerprint_verification = fingerprint_reconciler(
            config, normalized, fingerprint_mode, payload
        )
    except RuntimeError as exc:
        fingerprint_verification = fingerprint_verification_error(
            fingerprint_mode, codex_account_count, exc
        )
    after_accounts = accounts_loader(config)
    import_verification = verify_imported_accounts(
        normalized["accounts"], before_accounts, after_accounts, payload
    )
    return {
        "ok": True,
        "mode": mode,
        "upstream_status": status,
        "proxy_id": selected_proxy_id,
        "group_ids": selected_group_ids,
        "codex_fingerprint_mode": fingerprint_mode,
        "codex_account_count": codex_account_count,
        "fingerprint_verification": fingerprint_verification,
        "import_verification": import_verification,
        "result": payload_data(payload),
    }


def test_connection(config: dict[str, Any], *, request_json: JsonRequest) -> dict[str, Any]:
    if not config["admin_key"]:
        raise ValueError("请先配置 Sub2API 管理员密钥")
    status, payload, _ = request_json(
        "GET",
        config["base_url"] + "/api/v1/admin/accounts",
        headers={"x-api-key": config["admin_key"]},
        timeout=15,
    )
    accounts = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(accounts, dict):
        accounts = accounts.get("accounts") or accounts.get("items") or accounts.get("list")
    count = len(accounts) if isinstance(accounts, list) else None
    return {"ok": 200 <= status < 300, "upstream_status": status, "account_count": count}
