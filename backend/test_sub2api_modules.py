from __future__ import annotations

import unittest

from sub2api import automation, reclaim, routes, settings
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
            import_payload=lambda payload, **kwargs: imported.append((payload, kwargs)) or {"ok": True},
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        self.assertTrue(result["result"]["imported"])
        self.assertEqual(imported[0][1]["proxy_id"], 7)
        self.assertEqual(stored[0]["pending_card_codes"], [])

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
