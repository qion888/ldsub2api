"""Automatic reclaim and import orchestration for Sub2API."""

from __future__ import annotations

from typing import Any, Callable

from .reclaim import summarize_reclaim_result


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
    if not settings.get("enabled", False):
        return {"ok": True, "skipped": True, "reason": "disabled", "settings": settings, "state": state}

    pending = [
        str(value).strip()
        for value in state.get("pending_card_codes", [])
        if str(value).strip()
    ][:100]
    auto_import = bool(settings.get("auto_import", False))
    # Keep the completed-order ledger across queue transitions.  Clearing it
    # when pending work reaches zero causes the next cycle to download the same
    # completed reclaim files again.
    imported_order_nos = [
        str(order_no).strip()
        for order_no in state.get("imported_order_nos", [])
        if str(order_no).strip()
    ][-500:]
    if pending:
        refresh_kwargs = {"exclude_order_nos": imported_order_nos}
        if not auto_import:
            refresh_kwargs["include_downloads"] = False
        reclaim = refresh_reclaim(pending, **refresh_kwargs)
    else:
        reclaim_kwargs = {"exclude_order_nos": imported_order_nos}
        if not auto_import:
            reclaim_kwargs["include_downloads"] = False
        reclaim = reclaim_accounts(**reclaim_kwargs)
    if not isinstance(reclaim, dict):
        raise RuntimeError("401 找回返回格式无效")

    result = reclaim.get("result") if isinstance(reclaim.get("result"), dict) else {}
    downloads = reclaim.get("downloaded_payloads")
    downloads = downloads if isinstance(downloads, list) else []
    card_codes = reclaim.get("reclaim_card_codes")
    card_codes = (
        [str(value).strip() for value in card_codes if str(value).strip()][:100]
        if isinstance(card_codes, list)
        else pending
    )
    transport_ok = bool(reclaim.get("ok", result.get("ok", False)))
    summary_result = result if "ok" in result else {**result, "ok": transport_ok}
    metadata = summarize_reclaim_result(
        summary_result,
        submitted_card_codes=card_codes,
        downloaded_payloads=downloads,
        missing_card_code_accounts=(
            reclaim.get("missing_card_code_accounts")
            if isinstance(reclaim.get("missing_card_code_accounts"), list)
            else []
        ),
        exclude_order_nos=imported_order_nos,
        downloads_requested=auto_import,
    )
    # Prefer fields produced by the wrapper, while keeping injected/legacy
    # reclaim implementations compatible with the same normalized contract.
    for key, value in metadata.items():
        if key not in reclaim:
            reclaim[key] = value
    metadata = {**metadata, **{key: reclaim[key] for key in metadata if key in reclaim}}

    raw_retryable = reclaim.get("retryable_card_codes")
    if not isinstance(raw_retryable, list):
        raw_retryable = metadata["retryable_card_codes"]
    raw_permanent = []
    for key in (
        "permanent_card_codes",
        "unrecoverable_card_codes",
        "non_retryable_card_codes",
        "not_owned_card_codes",
        "skipped_card_codes",
    ):
        values = reclaim.get(key)
        if isinstance(values, list):
            raw_permanent.extend(values)
    permanent_codes = {
        str(value).strip() for value in raw_permanent if str(value).strip()
    }
    retryable_codes = [
        str(value).strip()
        for value in raw_retryable
        if str(value).strip() and str(value).strip() not in permanent_codes
    ][:100]
    raw_active_codes = reclaim.get("active_card_codes")
    if not isinstance(raw_active_codes, list):
        raw_active_codes = metadata.get("active_card_codes", [])
    active_codes = [
        str(value).strip()
        for value in raw_active_codes
        if str(value).strip() and str(value).strip() not in permanent_codes
    ][:100]
    active = _count(metadata["reclaim_summary"].get("active"))
    outcome = str(metadata.get("outcome") or "error")
    permanent_count = sum(
        _count(metadata["reclaim_summary"].get(key))
        for key in ("unreclaimable", "not_owned", "skipped")
    )

    # A failed request is still persisted with the scan context and submitted
    # codes, so the next run or the explicit retry action can recover it.
    if not transport_ok:
        error_message = str(
            result.get("error") or reclaim.get("error") or metadata.get("recovery_message") or "401 找回服务请求失败"
        )[:500]
        summary = {
            "ok": False,
            "outcome": "error",
            "recovery_status": "error",
            "recovery_ok": False,
            "recovery_message": error_message,
            "downloads_requested": auto_import,
            "scanned_accounts": reclaim.get("scanned_accounts"),
            "accounts_401": reclaim.get("accounts_401"),
            "card_code_count": reclaim.get("card_code_count", len(card_codes)),
            "retryable_card_codes": retryable_codes,
            "active_card_codes": metadata.get("active_card_codes", []),
            "retry_available": bool(retryable_codes),
            "reclaim_summary": metadata["reclaim_summary"],
            "reclaim_failures": metadata["reclaim_failures"],
            "import_status": "not_attempted",
            "import_error": "",
            "import_attempted": False,
            "imported": False,
            "import_result": None,
        }
        next_state = {
            "last_run": now(),
            "last_error": error_message,
            "last_result": summary,
            "pending_card_codes": retryable_codes[:100],
            "retryable_card_codes": retryable_codes[:100],
            "imported_order_nos": imported_order_nos,
            "run_history": (
                state.get("run_history", [])
                + [{"run_at": now(), "status": "error", **summary}]
            )[-20:],
        }
        store_state(next_state)
        return {"ok": False, "settings": settings, "state": next_state, "result": summary}

    import_result = None
    import_confirmed = False
    import_error = ""
    import_status = "disabled" if not auto_import else "not_attempted"
    importable = [
        item["data"]
        for item in downloads
        if isinstance(item, dict) and isinstance(item.get("data"), dict)
    ]
    if auto_import and importable:
        import_status = "failed"
        try:
            import_result = import_payload(
                importable,
                proxy_id=settings.get("proxy_id"),
                group_ids=settings.get("group_ids", []),
                codex_fingerprint_mode=settings.get("codex_fingerprint_mode", "off"),
                assign_existing=True,
            )
            verification = (
                import_result.get("import_verification")
                if isinstance(import_result, dict)
                else None
            )
            import_confirmed = bool(isinstance(verification, dict) and verification.get("confirmed"))
            import_status = "confirmed" if import_confirmed else "unconfirmed"
            if import_confirmed:
                imported_order_nos = list(dict.fromkeys(
                    imported_order_nos
                    + [str(item.get("task", {}).get("order_no") or "").strip() for item in downloads]
                ))[-500:]
        except Exception as exc:
            import_error = str(exc)[:500]
            import_result = {"ok": False, "error": import_error}

    if active > 0:
        known_pending = list(dict.fromkeys(active_codes + retryable_codes))
        # If the provider omitted task details and did not report a permanent
        # bucket, the submitted list is the only available polling context.
        next_pending = known_pending or (card_codes if not permanent_count else [])
    elif retryable_codes and outcome in {"failed", "partial", "error"}:
        next_pending = retryable_codes
    elif import_status in {"failed", "unconfirmed"}:
        # Keep only explicitly retryable reclaim work in the queue.  A failed
        # import is a separate concern and must not cause permanent 403 work
        # to be submitted again on every cycle.
        next_pending = retryable_codes
    elif outcome == "pending":
        next_pending = card_codes
    else:
        next_pending = []
    recovery_summary = metadata["reclaim_summary"]
    summary = {
        "ok": True,
        "outcome": outcome,
        "recovery_status": metadata["recovery_status"],
        "recovery_ok": metadata["recovery_ok"],
        "recovery_message": metadata["recovery_message"],
        "downloads_requested": auto_import,
        "scanned_accounts": reclaim.get("scanned_accounts"),
        "accounts_401": reclaim.get("accounts_401"),
        "card_code_count": reclaim.get("card_code_count", len(card_codes or [])),
        "queued": recovery_summary["queued"],
        "already_running": recovery_summary["already_running"],
        "done": recovery_summary["done"],
        "failed": recovery_summary["failed"],
        "unreclaimable": recovery_summary["unreclaimable"],
        "not_owned": recovery_summary["not_owned"],
        "skipped": recovery_summary["skipped"],
        "download_failed": recovery_summary["download_failed"],
        "download_skipped": recovery_summary.get("download_skipped", 0),
        "downloaded": len(downloads),
        "skipped_downloads": recovery_summary.get("download_skipped", 0),
        "reclaim_summary": recovery_summary,
        "reclaim_failures": metadata["reclaim_failures"],
        "retryable_card_codes": retryable_codes,
        "active_card_codes": active_codes,
        "retry_available": bool(metadata["retry_available"]),
        "import_status": import_status,
        "import_error": import_error,
        "import_attempted": bool(import_result),
        "imported": import_confirmed,
        "import_result": import_result,
    }
    history_status = "success" if outcome in {"recovered", "no_401"} and import_status not in {"failed", "unconfirmed"} else "partial"
    next_state = {
        "last_run": now(),
        "last_error": "",
        "last_result": summary,
        "pending_card_codes": next_pending,
        "retryable_card_codes": retryable_codes,
        "imported_order_nos": imported_order_nos,
        "run_history": (
            state.get("run_history", [])
            + [{"run_at": now(), "status": history_status, **summary}]
        )[-20:],
    }
    store_state(next_state)
    return {"ok": True, "settings": settings, "state": next_state, "result": summary}
