from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monitor_core import database as monitor_database
from sub2api import automation, card_import_history, client, reclaim, routes, settings
from sub2api.worker import Sub2ApiAutomationWorker


class Sub2ApiModuleTests(unittest.TestCase):
    def test_card_import_history_tracks_pending_success_and_failure_totals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"

            def database():
                return monitor_database.create_database(path)

            timestamps = iter((
                "2026-08-30T08:00:00+00:00",
                "2026-08-30T08:01:00+00:00",
                "2026-08-30T08:02:00+00:00",
                "2026-08-30T08:03:00+00:00",
                "2026-08-30T08:04:00+00:00",
            ))
            now = lambda: next(timestamps)
            card_import_history.initialize(database)
            successful = card_import_history.create_record(
                database, {"mode": "auto", "card_count": 3}, now=now
            )
            running_result = card_import_history.list_records(database, {"status": ["pending"]})
            card_import_history.update_record(
                database,
                successful["id"],
                {
                    "status": "pending",
                    "stage": "ready",
                    "verified_count": 3,
                    "downloaded_files": 2,
                    "account_count": 2,
                },
                now=now,
            )
            pending_result = card_import_history.list_records(database, {"status": ["pending"]})
            completed = card_import_history.update_record(
                database,
                successful["id"],
                {
                    "status": "success",
                    "stage": "done",
                    "success_count": 2,
                    "details": {"new_account_ids": [11, "12", -1]},
                    "message": "推送并核验成功",
                },
                now=now,
            )
            failed = card_import_history.create_record(
                database, {"mode": "manual", "card_count": 1}, now=now
            )
            card_import_history.update_record(
                database,
                failed["id"],
                {
                    "status": "failed",
                    "stage": "error",
                    "failed_count": 1,
                    "message": "上游拒绝导入",
                    "details": {"missing_accounts": [{"name": "account-a", "count": 1}]},
                },
                now=now,
            )

            result = card_import_history.list_records(database, {"limit": ["20"]})

        self.assertEqual(running_result["items"][0]["status"], "running")
        self.assertEqual(pending_result["total"], 1)
        self.assertEqual(pending_result["items"][0]["status"], "pending")
        self.assertEqual(completed["details"]["new_account_ids"], [11, 12])
        self.assertEqual(result["summary"], {
            "total": 2,
            "success": 1,
            "failed": 1,
            "pending": 0,
            "successful_accounts": 2,
            "failed_accounts": 1,
        })
        self.assertEqual(result["items"][0]["message"], "上游拒绝导入")
        self.assertEqual(result["items"][1]["status"], "success")

    def test_settings_are_normalized_through_injected_storage(self) -> None:
        stored = []
        value = settings.save_automation_settings(
            {
                "enabled": True,
                "interval_seconds": "30",
                "auto_import": True,
                "proxy_id": "7",
                "group_ids": [9, "3", 9],
                "codex_fingerprint_mode": "SESSION",
            },
            has_admin_key=lambda: True,
            store_setting=lambda key, data: stored.append((key, data)),
        )

        self.assertEqual(value["group_ids"], [3, 9])
        self.assertEqual(value["codex_fingerprint_mode"], "session")
        self.assertEqual(stored, [("sub2api_automation", value)])

    def test_401_detection_only_accepts_error_context(self) -> None:
        self.assertTrue(
            reclaim.account_is_401(
                {"status": "error", "error_message": "Token revoked (401)"}
            )
        )
        self.assertFalse(
            reclaim.account_is_401(
                {"name": "user401", "error_message": "upstream timeout"}
            )
        )

    def test_automation_cycle_uses_injected_operations(self) -> None:
        stored = []
        imported = []
        state = {
            "last_run": None,
            "last_error": "",
            "last_result": None,
            "pending_card_codes": ["team-CARD-1"],
            "imported_order_nos": [],
            "run_history": [],
        }
        result = automation.run_cycle(
            settings_loader=lambda: {
                "enabled": True,
                "interval_seconds": 30,
                "auto_import": True,
                "proxy_id": 7,
                "group_ids": [3],
                "codex_fingerprint_mode": "device",
            },
            state_loader=lambda: state,
            refresh_reclaim=lambda codes, **kwargs: {
                "ok": True,
                "reclaim_card_codes": codes,
                "downloaded_payloads": [
                    {
                        "task": {"order_no": "ORDER-1"},
                        "data": {"accounts": [{"credentials": {"token": "value"}}]},
                    }
                ],
                "result": {"queued": 0, "done": 1},
            },
            reclaim_accounts=lambda **kwargs: self.fail("pending codes must refresh progress"),
            import_payload=lambda payload, **kwargs: imported.append((payload, kwargs)) or {
                "ok": True,
                "import_verification": {"confirmed": True, "matched": 1, "expected": 1},
            },
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        self.assertTrue(result["result"]["imported"])
        self.assertEqual(imported[0][1]["proxy_id"], 7)
        self.assertEqual(stored[0]["pending_card_codes"], [])

    def test_automation_keeps_completed_order_ledger_and_excludes_it_next_cycle(self) -> None:
        state = {
            "pending_card_codes": [],
            "imported_order_nos": ["ORDER-1"],
            "run_history": [],
        }
        calls = []
        result = automation.run_cycle(
            settings_loader=lambda: {"enabled": True, "proxy_id": 7, "group_ids": [3], "codex_fingerprint_mode": "device"},
            state_loader=lambda: state,
            reclaim_accounts=lambda **kwargs: calls.append(kwargs) or {
                "ok": True,
                "reclaim_card_codes": [],
                "downloaded_payloads": [],
                "result": {
                    "queued": 0,
                    "already_running": 0,
                    "done": 1,
                    "all_tasks": [{"status": "done", "order_no": "ORDER-1"}],
                },
            },
            refresh_reclaim=lambda *args, **kwargs: self.fail("pending codes must use account scan"),
            import_payload=lambda *args, **kwargs: self.fail("excluded order must not import"),
            store_state=lambda value: calls.append(value),
            now=lambda: "2026-08-30T08:00:00+00:00",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0]["exclude_order_nos"], ["ORDER-1"])
        self.assertEqual(calls[-1]["imported_order_nos"], ["ORDER-1"])
        self.assertEqual(result["result"]["skipped_downloads"], 1)

    def test_import_verification_only_counts_new_matching_accounts(self) -> None:
        summary = client.verify_imported_accounts(
            [{"name": "same", "platform": "openai", "type": "oauth"}],
            [{"id": 10, "name": "same", "platform": "openai", "type": "oauth"}],
            [
                {"id": 10, "name": "same", "platform": "openai", "type": "oauth"},
                {"id": 11, "name": "same", "platform": "openai", "type": "oauth"},
            ],
            {"data": {"success": 1, "failed": 0}},
        )
        self.assertTrue(summary["confirmed"])
        self.assertEqual(summary["new_account_ids"], [11])

    def test_account_page_redacts_credentials_and_mounts_usage(self) -> None:
        calls = []

        def request_json(method, endpoint, payload=None, **kwargs):
            calls.append((method, endpoint, payload, kwargs))
            if method == "GET":
                return 200, {"data": {"items": [{
                    "id": 4, "name": "A", "platform": "openai", "type": "oauth",
                    "credentials": {"access_token": "secret"}, "quota_limit": 10,
                    "quota_used": 2, "rate_limited_at": "2026-08-30T08:00:00Z",
                    "quota_daily_reset_at": "2026-08-31T00:00:00Z",
                    "five_hour": {"utilization": 10, "internal_state": "hidden"},
                    "groups": [{"id": 2, "name": "Codex"}],
                }], "total": 1}}, ""
            return 200, {"data": {"usage": {"4": {
                "source": "passive",
                "five_hour": {
                    "utilization": 42.5,
                    "resets_at": "2026-08-30T12:00:00Z",
                    "remaining_seconds": 1200,
                    "window_stats": {
                        "requests": 12, "tokens": 3456, "cost": 1.2,
                        "standard_cost": 1.0, "user_cost": 1.5,
                        "internal_trace": "hidden",
                    },
                    "internal_state": "hidden",
                },
                "credentials": {"access_token": "hidden"},
            }}, "errors": {}}}, ""

        result = client.fetch_account_page(
            {"base_url": "https://sub2api.example", "admin_key": "secret"},
            request_json=request_json,
            search="A",
        )
        self.assertEqual(result["items"][0]["id"], 4)
        self.assertNotIn("credentials", result["items"][0])
        self.assertEqual(result["items"][0]["rate_limited_at"], "2026-08-30T08:00:00Z")
        self.assertEqual(result["items"][0]["quota_daily_reset_at"], "2026-08-31T00:00:00Z")
        self.assertEqual(result["items"][0]["five_hour"], {"utilization": 10})
        usage = result["usage"]["4"]
        self.assertEqual(usage["five_hour"]["utilization"], 42.5)
        self.assertEqual(usage["five_hour"]["window_stats"]["tokens"], 3456)
        self.assertNotIn("internal_state", usage["five_hour"])
        self.assertNotIn("internal_trace", usage["five_hour"]["window_stats"])
        self.assertNotIn("credentials", usage)

    def test_sse_account_test_uses_final_completion_event(self) -> None:
        def request_json(method, endpoint, payload=None, **kwargs):
            return 200, {}, "\n".join([
                'data: {"type":"test_start"}',
                'data: {"type":"content","text":"connected"}',
                'data: {"type":"test_complete","success":false,"error":"rate limited"}',
            ])

        tested = client.test_account(
            {"base_url": "https://sub2api.example", "admin_key": "secret"},
            9,
            request_json=request_json,
        )
        self.assertFalse(tested["ok"])
        self.assertEqual(tested["message"], "rate limited")

    def test_sse_account_test_and_delete_use_admin_endpoints(self) -> None:
        seen = []

        def request_json(method, endpoint, payload=None, **kwargs):
            seen.append((method, endpoint, payload))
            if method == "POST":
                return 200, {}, 'data: {"success": true, "latency_ms": 42}\n'
            return 204, {}, ""

        tested = client.test_account({"base_url": "https://sub2api.example", "admin_key": "secret"}, 9, request_json=request_json)
        deleted = client.delete_account({"base_url": "https://sub2api.example", "admin_key": "secret"}, 9, request_json=request_json)
        self.assertTrue(tested["ok"])
        self.assertTrue(deleted["ok"])
        self.assertEqual(seen[0][1], "https://sub2api.example/api/v1/admin/accounts/9/test")
        self.assertEqual(seen[1][1], "https://sub2api.example/api/v1/admin/accounts/9")

    def test_routes_are_independent_from_main_handler(self) -> None:
        responses = []
        handled = routes.handle_get(
            "/api/sub2api/options",
            send_json=lambda payload, status=200: responses.append((status, payload)),
            settings_loader=lambda **kwargs: {},
            automation_settings_loader=lambda: {},
            automation_state_loader=lambda: {},
            options_loader=lambda: (_ for _ in ()).throw(ValueError("missing key")),
        )

        self.assertTrue(handled)
        self.assertEqual(responses, [(400, {"detail": "missing key"})])

    def test_account_routes_dispatch_query_test_and_delete(self) -> None:
        responses = []
        self.assertTrue(routes.handle_get(
            "/api/sub2api/accounts",
            send_json=lambda payload, status=200: responses.append((status, payload)),
            settings_loader=lambda **kwargs: {},
            automation_settings_loader=lambda: {},
            automation_state_loader=lambda: {},
            options_loader=lambda: {},
            account_loader=lambda query: {"query": query},
            query_values={"search": ["alice"]},
        ))
        self.assertEqual(responses[-1], (200, {"query": {"search": ["alice"]}}))
        self.assertTrue(routes.handle_post(
            "/api/sub2api/accounts/12/test", {},
            send_json=lambda payload, status=200: responses.append((status, payload)),
            test_connection=lambda: {},
            reclaim_accounts=lambda **kwargs: {},
            refresh_reclaim=lambda *args, **kwargs: {},
            run_automation=lambda: {},
            import_payload=lambda *args, **kwargs: {},
            test_account=lambda account_id: {"ok": True, "account_id": account_id},
        ))
        self.assertEqual(responses[-1], (200, {"ok": True, "account_id": 12}))

    def test_card_import_history_routes_dispatch_create_update_and_list(self) -> None:
        responses = []
        common_get = {
            "send_json": lambda payload, status=200: responses.append((status, payload)),
            "settings_loader": lambda **kwargs: {},
            "automation_settings_loader": lambda: {},
            "automation_state_loader": lambda: {},
            "options_loader": lambda: {},
        }
        self.assertTrue(routes.handle_get(
            "/api/sub2api/card-import-records",
            **common_get,
            card_import_history_loader=lambda query: {"query": query},
            query_values={"status": ["failed"]},
        ))
        self.assertEqual(responses[-1], (200, {"query": {"status": ["failed"]}}))

        common_write = {
            "send_json": common_get["send_json"],
            "test_connection": lambda: {},
            "reclaim_accounts": lambda **kwargs: {},
            "refresh_reclaim": lambda *args, **kwargs: {},
            "run_automation": lambda: {},
            "import_payload": lambda *args, **kwargs: {},
        }
        self.assertTrue(routes.handle_post(
            "/api/sub2api/card-import-records",
            {"mode": "auto", "card_count": 2},
            **common_write,
            card_import_history_creator=lambda payload: {"id": 7, **payload},
        ))
        self.assertEqual(responses[-1][0], 201)
        self.assertEqual(responses[-1][1]["id"], 7)

        self.assertTrue(routes.handle_put(
            "/api/sub2api/card-import-records/7",
            {"status": "success"},
            send_json=common_get["send_json"],
            normalize_url=lambda value, default: default,
            store_setting=lambda key, value: None,
            settings_loader=lambda **kwargs: {},
            save_automation_settings=lambda payload: payload,
            automation_state_loader=lambda: {},
            card_import_history_updater=lambda record_id, payload: {"id": record_id, **payload},
        ))
        self.assertEqual(responses[-1], (200, {"id": 7, "status": "success"}))
        self.assertTrue(routes.handle_delete(
            "/api/sub2api/accounts/12",
            send_json=lambda payload, status=200: responses.append((status, payload)),
            delete_account=lambda account_id: {"ok": True, "account_id": account_id},
        ))
        self.assertEqual(responses[-1], (200, {"ok": True, "account_id": 12}))

    def test_worker_records_failure_through_injected_state_store(self) -> None:
        stored = []
        worker = Sub2ApiAutomationWorker(
            run_cycle=lambda: (_ for _ in ()).throw(RuntimeError("upstream failed")),
            settings_loader=lambda: {"enabled": False},
            state_loader=lambda: {
                "last_result": None,
                "pending_card_codes": [],
                "imported_order_nos": [],
                "run_history": [],
            },
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        result = worker.run_once()

        self.assertFalse(result["ok"])
        self.assertEqual(stored[0]["last_error"], "upstream failed")
        self.assertEqual(stored[0]["run_history"][0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
