"""HTTP-facing Sub2API client operations."""

from __future__ import annotations

import json
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
    return {
        "ok": True,
        "mode": mode,
        "upstream_status": status,
        "proxy_id": selected_proxy_id,
        "group_ids": selected_group_ids,
        "codex_fingerprint_mode": fingerprint_mode,
        "codex_account_count": codex_account_count,
        "fingerprint_verification": fingerprint_verification,
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
