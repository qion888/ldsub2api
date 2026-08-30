from __future__ import annotations

import unittest

from sub2api import automation, client, reclaim, routes, settings
from sub2api.worker import Sub2ApiAutomationWorker


class Sub2ApiModuleTests(unittest.TestCase):
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
                    "quota_used": 2, "groups": [{"id": 2, "name": "Codex"}],
                }], "total": 1}}, ""
            return 200, {"data": {"usage": {"4": {"five_hour": {"used": 1, "limit": 5}}}}}, ""

        result = client.fetch_account_page(
            {"base_url": "https://sub2api.example", "admin_key": "secret"},
            request_json=request_json,
            search="A",
        )
        self.assertEqual(result["items"][0]["id"], 4)
        self.assertNotIn("credentials", result["items"][0])
        self.assertEqual(result["usage"]["4"]["five_hour"]["limit"], 5)

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
