"""HTTP route handlers for the Sub2API integration."""

from __future__ import annotations

import re
from typing import Any, Callable

from .card_import_history import RetryFailed, RetryNotAllowed
from .constants import DEFAULT_URL

SendJson = Callable[[Any, int], Any]


def handle_get(
    path: str,
    *,
    send_json: SendJson,
    settings_loader: Callable[..., dict[str, Any]],
    automation_settings_loader: Callable[[], dict[str, Any]],
    automation_state_loader: Callable[[], dict[str, Any]],
    options_loader: Callable[[], dict[str, Any]],
    account_loader: Callable[[dict[str, list[str]]], dict[str, Any]] | None = None,
    card_import_history_loader: Callable[[dict[str, list[str]]], dict[str, Any]] | None = None,
    query_values: dict[str, list[str]] | None = None,
) -> bool:
    if path == "/api/sub2api/config":
        send_json(settings_loader(), 200)
        return True
    if path == "/api/sub2api/automation":
        send_json(
            {
                "ok": True,
                "settings": automation_settings_loader(),
                "state": automation_state_loader(),
            },
            200,
        )
        return True
    if path == "/api/sub2api/options":
        try:
            send_json(options_loader(), 200)
        except ValueError as exc:
            send_json({"detail": str(exc)}, 400)
        except RuntimeError as exc:
            send_json({"detail": str(exc)}, 502)
        return True
    if path == "/api/sub2api/accounts":
        if account_loader is None:
            return False
        try:
            send_json(account_loader(query_values or {}), 200)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        except RuntimeError as exc:
            send_json({"detail": str(exc)}, 502)
        return True
    if path == "/api/sub2api/card-import-records":
        if card_import_history_loader is None:
            return False
        try:
            send_json(card_import_history_loader(query_values or {}), 200)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        return True
    return False


def handle_post(
    path: str,
    data: dict[str, Any],
    *,
    send_json: SendJson,
    test_connection: Callable[[], dict[str, Any]],
    reclaim_accounts: Callable[..., dict[str, Any]],
    refresh_reclaim: Callable[..., dict[str, Any]],
    run_automation: Callable[[], dict[str, Any]],
    import_payload: Callable[..., dict[str, Any]],
    test_account: Callable[[int], dict[str, Any]] | None = None,
    card_import_history_creator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    card_import_history_retry: Callable[[int], dict[str, Any]] | None = None,
    card_import_history_deleter: Callable[[int], dict[str, Any]] | None = None,
    card_import_history_batch_deleter: Callable[[list[int]], dict[str, Any]] | None = None,
) -> bool:
    if path == "/api/sub2api/card-import-records/delete":
        if card_import_history_deleter is None:
            return False
        try:
            send_json(card_import_history_deleter(data.get("id")), 200)
        except LookupError as exc:
            send_json({"detail": str(exc)}, 404)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        return True

    if path == "/api/sub2api/card-import-records/batch-delete":
        if card_import_history_batch_deleter is None:
            return False
        try:
            send_json(card_import_history_batch_deleter(data.get("ids")), 200)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        return True

    if path == "/api/sub2api/card-import-records":
        if card_import_history_creator is None:
            return False
        try:
            send_json(card_import_history_creator(data), 201)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        return True

    retry_match = re.fullmatch(r"/api/sub2api/card-import-records/(\d+)/retry", path)
    if retry_match:
        if card_import_history_retry is None:
            return False
        try:
            result = card_import_history_retry(int(retry_match.group(1)))
            send_json(result, 200 if result.get("ok", False) else 502)
        except LookupError as exc:
            send_json({"detail": str(exc)}, 404)
        except RetryNotAllowed as exc:
            send_json({"detail": str(exc)}, 409)
        except RetryFailed as exc:
            send_json({"detail": str(exc)}, 502)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        except Exception as exc:
            send_json({"detail": str(exc)[:240]}, 502)
        return True

    if path == "/api/sub2api/test":
        try:
            send_json(test_connection(), 200)
        except ValueError as exc:
            send_json({"detail": str(exc)}, 400)
        except RuntimeError as exc:
            send_json({"detail": str(exc)}, 502)
        return True

    account_test = re.fullmatch(r"/api/sub2api/accounts/(\d+)/test", path)
    if account_test:
        if test_account is None:
            return False
        try:
            send_json(test_account(int(account_test.group(1))), 200)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        except RuntimeError as exc:
            send_json({"detail": str(exc)}, 502)
        return True

    if path in ("/api/sub2api/reclaim-401", "/api/sub2api/reclaim401"):
        try:
            exclude_order_nos = data.get("exclude_order_nos")
            result = (
                reclaim_accounts(exclude_order_nos=exclude_order_nos)
                if isinstance(exclude_order_nos, list)
                else reclaim_accounts()
            )
            if not isinstance(result, dict):
                send_json({"detail": "401 找回返回格式无效"}, 502)
                return True
            if not result.get("ok", False):
                failure = result.get("result") if isinstance(result.get("result"), dict) else {}
                detail = str(
                    failure.get("error") or result.get("error") or "401 找回服务返回失败"
                )[:240]
                result = {**result, "detail": detail}
            send_json(result, 200 if result.get("ok", False) else 502)
        except ValueError as exc:
            send_json({"detail": str(exc)}, 400)
        except RuntimeError as exc:
            send_json({"detail": str(exc)}, 502)
        return True

    if path == "/api/sub2api/reclaim-progress":
        raw_codes = data.get("card_codes")
        if not isinstance(raw_codes, list):
            send_json({"detail": "请提供 card_codes 数组"}, 400)
            return True
        try:
            exclude_order_nos = data.get("exclude_order_nos")
            result = (
                refresh_reclaim(raw_codes, exclude_order_nos=exclude_order_nos)
                if isinstance(exclude_order_nos, list)
                else refresh_reclaim(raw_codes)
            )
            send_json(result, 200 if result["ok"] else 502)
        except ValueError as exc:
            send_json({"detail": str(exc)}, 400)
        except RuntimeError as exc:
            send_json({"detail": str(exc)}, 502)
        return True

    if path == "/api/sub2api/automation/run":
        result = run_automation()
        send_json(result, 200 if result.get("ok", False) else 502)
        return True

    if path == "/api/sub2api/import":
        try:
            source = data.get("data") if "data" in data else data
            result = import_payload(
                source,
                proxy_id=data.get("proxy_id"),
                group_ids=data.get("group_ids"),
                codex_fingerprint_mode=(
                    data.get("codex_fingerprint_mode")
                    if "codex_fingerprint_mode" in data
                    else None
                ),
                assign_existing=data.get("assign_existing") if "assign_existing" in data else None,
                endpoint=str(data.get("endpoint") or "/api/v1/admin/accounts/data"),
                reclaim_order_nos=data.get("reclaim_order_nos"),
            )
            send_json(result, 200)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        except RuntimeError as exc:
            send_json({"detail": str(exc)}, 502)
        return True
    return False


def handle_delete(
    path: str,
    *,
    send_json: SendJson,
    delete_account: Callable[[int], dict[str, Any]] | None = None,
    card_import_history_deleter: Callable[[int], dict[str, Any]] | None = None,
) -> bool:
    card_import_match = re.fullmatch(r"/api/sub2api/card-import-records/(\d+)", path)
    if card_import_match:
        if card_import_history_deleter is None:
            return False
        try:
            send_json(card_import_history_deleter(int(card_import_match.group(1))), 200)
        except LookupError as exc:
            send_json({"detail": str(exc)}, 404)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        return True

    account_match = re.fullmatch(r"/api/sub2api/accounts/(\d+)", path)
    if not account_match:
        return False
    if delete_account is None:
        return False
    try:
        send_json(delete_account(int(account_match.group(1))), 200)
    except (TypeError, ValueError) as exc:
        send_json({"detail": str(exc)}, 400)
    except RuntimeError as exc:
        send_json({"detail": str(exc)}, 502)
    return True


def handle_put(
    path: str,
    data: dict[str, Any],
    *,
    send_json: SendJson,
    normalize_url: Callable[[Any, str], str],
    store_setting: Callable[[str, dict[str, Any]], None],
    settings_loader: Callable[..., dict[str, Any]],
    save_automation_settings: Callable[[dict[str, Any]], dict[str, Any]],
    automation_state_loader: Callable[[], dict[str, Any]],
    card_import_history_updater: Callable[[int, dict[str, Any]], dict[str, Any]] | None = None,
) -> bool:
    card_import_match = re.fullmatch(r"/api/sub2api/card-import-records/(\d+)", path)
    if card_import_match:
        if card_import_history_updater is None:
            return False
        try:
            send_json(card_import_history_updater(int(card_import_match.group(1)), data), 200)
        except LookupError as exc:
            send_json({"detail": str(exc)}, 404)
        except (TypeError, ValueError) as exc:
            send_json({"detail": str(exc)}, 400)
        return True

    if path == "/api/sub2api/config":
        try:
            base_url = normalize_url(data.get("base_url"), DEFAULT_URL)
        except ValueError as exc:
            send_json({"detail": str(exc)}, 400)
            return True
        supplied_key = data.get("admin_key")
        current = settings_loader(reveal=True)
        if supplied_key is None or str(supplied_key).strip() == "":
            admin_key = current["admin_key"]
        else:
            admin_key = str(supplied_key).strip()
        if len(admin_key) > 512:
            send_json({"detail": "管理员密钥过长"}, 400)
            return True
        store_setting("sub2api", {"base_url": base_url, "admin_key": admin_key})
        send_json(settings_loader(), 200)
        return True

    if path == "/api/sub2api/automation":
        try:
            settings = save_automation_settings(data)
        except ValueError as exc:
            send_json({"detail": str(exc)}, 400)
            return True
        send_json(
            {"ok": True, "settings": settings, "state": automation_state_loader()},
            200,
        )
        return True
    return False
