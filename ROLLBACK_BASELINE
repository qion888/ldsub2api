"""Automatic reclaim and import orchestration for Sub2API."""

from __future__ import annotations

from typing import Any, Callable


def run_cycle(
    *,
    settings_loader: Callable[[], dict[str, Any]],
    state_loader: Callable[[], dict[str, Any]],
    refresh_reclaim: Callable[..., dict[str, Any]],
    reclaim_accounts: Callable[..., dict[str, Any]],
    import_payload: Callable[..., dict[str, Any]],
    store_state: Callable[[dict[str, Any]], None],
    now: Callable[[], str],
) -> dict[str, Any]:
    settings = settings_loader()
    state = state_loader()
    if not settings["enabled"]:
        return {"ok": True, "skipped": True, "reason": "disabled", "settings": settings, "state": state}

    pending = state["pending_card_codes"]
    imported_order_nos = state["imported_order_nos"] if pending else []
    if pending:
        reclaim = refresh_reclaim(pending, exclude_order_nos=imported_order_nos)
    else:
        reclaim = reclaim_accounts(include_downloads=True, exclude_order_nos=imported_order_nos)
    if not reclaim.get("ok", False):
        failure = reclaim.get("result") if isinstance(reclaim.get("result"), dict) else {}
        raise RuntimeError(
            str(failure.get("error") or reclaim.get("error") or "401 找回服务返回失败")[:500]
        )

    result = reclaim.get("result") if isinstance(reclaim.get("result"), dict) else {}
    downloads = reclaim.get("downloaded_payloads")
    downloads = downloads if isinstance(downloads, list) else []
    card_codes = reclaim.get("reclaim_card_codes")
    card_codes = card_codes if isinstance(card_codes, list) else pending
    import_result = None
    if downloads:
        import_result = import_payload(
            [
                item["data"]
                for item in downloads
                if isinstance(item, dict) and isinstance(item.get("data"), dict)
            ],
            proxy_id=settings["proxy_id"],
            group_ids=settings["group_ids"],
            codex_fingerprint_mode=settings["codex_fingerprint_mode"],
            assign_existing=True,
        )
        imported_order_nos = (
            imported_order_nos
            + [str(item.get("task", {}).get("order_no") or "") for item in downloads]
        )[-500:]

    if int(result.get("queued") or 0) + int(result.get("already_running") or 0) > 0:
        pending = card_codes
    else:
        pending = []
        imported_order_nos = []
    summary = {
        "ok": bool(reclaim.get("ok", False)),
        "scanned_accounts": reclaim.get("scanned_accounts"),
        "accounts_401": reclaim.get("accounts_401"),
        "card_code_count": reclaim.get("card_code_count", len(card_codes or [])),
        "queued": int(result.get("queued") or 0),
        "done": int(result.get("done") or 0),
        "downloaded": len(downloads),
        "imported": bool(import_result),
        "import_result": import_result,
    }
    state = {
        "last_run": now(),
        "last_error": "",
        "last_result": summary,
        "pending_card_codes": pending,
        "imported_order_nos": imported_order_nos,
        "run_history": (
            state.get("run_history", [])
            + [{"run_at": now(), "status": "success", **summary}]
        )[-20:],
    }
    store_state(state)
    return {"ok": True, "settings": settings, "state": state, "result": summary}
