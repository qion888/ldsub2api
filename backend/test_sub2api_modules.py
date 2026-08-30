from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monitor_core import database as monitor_database
from sub2api import automation, card_import_history, client, reclaim, routes, settings
from sub2api.worker import Sub2ApiAutomationWorker


class Sub2ApiModuleTests(unittest.TestCase):
    def test_card_import_history_migrates_existing_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"

            def database():
                return monitor_database.create_database(path)

            with database() as connection:
                connection.execute(
                    """
                    CREATE TABLE sub2api_card_import_records (
                        id INTEGER PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        card_count INTEGER NOT NULL DEFAULT 0,
                        verified_count INTEGER NOT NULL DEFAULT 0,
                        downloaded_files INTEGER NOT NULL DEFAULT 0,
                        account_count INTEGER NOT NULL DEFAULT 0,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        failed_count INTEGER NOT NULL DEFAULT 0,
                        filename TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        details_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )

            card_import_history.initialize(database)
            with database() as connection:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(sub2api_card_import_records)"
                    )
                }

        self.assertIn("card_codes_json", columns)
        self.assertIn("retry_context_json", columns)

    def test_card_import_history_paginates_codes_and_retries_private_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"

            def database():
                return monitor_database.create_database(path)

            timestamps = iter(f"2026-08-30T08:{minute:02d}:00+00:00" for minute in range(12))
            now = lambda: next(timestamps)
            card_import_history.initialize(database)
            records = [
                card_import_history.create_record(
                    database,
                    {"mode": "manual", "card_codes": [f"CARD-{index}"]},
                    now=now,
                )
                for index in range(3)
            ]
            retry_context = {
                "data": {"accounts": [{"name": "account-a"}]},
                "assign_existing": True,
                "proxy_id": 7,
                "group_ids": [3, 4],
                "codex_fingerprint_mode": "session",
                "endpoint": "/api/v1/admin/accounts/data",
                "reclaim_order_nos": ["ORDER-123"],
            }
            pending = card_import_history.update_record(
                database,
                records[-1]["id"],
                {"status": "pending", "stage": "ready", "retry_context": retry_context},
                now=now,
            )
            first_page = card_import_history.list_records(
                database, {"page": ["1"], "page_size": ["2"]}
            )
            second_page = card_import_history.list_records(
                database, {"page": ["2"], "page_size": ["2"]}
            )
            calls = []
            retried = card_import_history.retry_record(
                database,
                pending["id"],
                import_payload=lambda data, **kwargs: calls.append((data, kwargs)) or {
                    "import_verification": {
                        "confirmed": True,
                        "expected": 1,
                        "matched": 1,
                        "failed": 0,
                        "new_account_ids": [42],
                    }
                },
                now=now,
            )

        self.assertEqual(first_page["items"][0]["card_codes"], ["CARD-2"])
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(first_page["pages"], 2)
        self.assertEqual(second_page["page"], 2)
        self.assertEqual([item["card_codes"][0] for item in second_page["items"]], ["CARD-0"])
        self.assertNotIn("retry_context", pending)
        self.assertTrue(pending["retryable"])
        self.assertTrue(retried["ok"])
        self.assertEqual(retried["record"]["status"], "success")
        self.assertFalse(retried["record"]["retryable"])
        self.assertEqual(calls[0][0], retry_context["data"])
        self.assertEqual(calls[0][1]["proxy_id"], 7)
        self.assertEqual(calls[0][1]["reclaim_order_nos"], ["ORDER-123"])

    def test_card_import_history_deletes_single_and_multiple_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"

            def database():
                return monitor_database.create_database(path)

            timestamps = iter(f"2026-08-30T09:{minute:02d}:00+00:00" for minute in range(4))
            card_import_history.initialize(database)
            records = [
                card_import_history.create_record(
                    database,
                    {"mode": "manual", "card_codes": [f"DELETE-{index}"]},
                    now=lambda: next(timestamps),
                )
                for index in range(3)
            ]

            single = card_import_history.delete_record(database, records[0]["id"])
            batch = card_import_history.delete_records(
                database, [records[1]["id"], records[2]["id"], records[2]["id"], 999]
            )
            remaining = card_import_history.list_records(database)
            self.assertEqual(single["deleted_ids"], [records[0]["id"]])
            self.assertEqual(batch["requested_count"], 3)
            self.assertEqual(batch["deleted_count"], 2)
            self.assertEqual(batch["deleted_ids"], [records[1]["id"], records[2]["id"]])
            self.assertEqual(remaining["total"], 0)
            with self.assertRaises(card_import_history.RecordNotFound):
                card_import_history.delete_record(database, records[0]["id"])
            for invalid in ([], [0], ["bad"], list(range(1, 502))):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    card_import_history.delete_records(database, invalid)

    def test_card_import_history_retry_failure_restores_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"

            def database():
                return monitor_database.create_database(path)

            timestamps = iter((
                "2026-08-30T09:00:00+00:00",
                "2026-08-30T09:01:00+00:00",
                "2026-08-30T09:02:00+00:00",
                "2026-08-30T09:03:00+00:00",
            ))
            now = lambda: next(timestamps)
            card_import_history.initialize(database)
            record = card_import_history.create_record(
                database, {"mode": "auto", "card_codes": ["FAILED-CARD"]}, now=now
            )
            card_import_history.update_record(
                database,
                record["id"],
                {
                    "status": "failed",
                    "stage": "error",
                    "retry_context": {"data": {"accounts": [{"name": "account-b"}]}},
                },
                now=now,
            )

            with self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
                card_import_history.retry_record(
                    database,
                    record["id"],
                    import_payload=lambda *args, **kwargs: (_ for _ in ()).throw(
                        RuntimeError("upstream unavailable")
                    ),
                    now=now,
                )
            failed = card_import_history.list_records(database)["items"][0]

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "error")
        self.assertIn("upstream unavailable", failed["message"])
        self.assertTrue(failed["retryable"])

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
                "2026-08-30T08:05:00+00:00",
            ))
            now = lambda: next(timestamps)
            card_import_history.initialize(database)
            successful = card_import_history.create_record(
                database, {"mode": "auto", "card_count": 3}, now=now
            )
            running_result = card_import_history.list_records(database, {"status": ["pending"]})
            polling = card_import_history.update_record(
                database,
                successful["id"],
                {"status": "running", "stage": "poll", "message": "等待账号 JSON"},
                now=now,
            )
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
        self.assertEqual(polling["stage"], "poll")
        self.assertIn("waiting", card_import_history.STAGES)
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

    def test_reclaim_summary_separates_permanent_and_retryable_tasks(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "failed": 2,
                "all_tasks": [
                    {
                        "card_code": "CARD-403",
                        "status": "failed",
                        "permanent": True,
                        "error_code": "account_deactivated",
                        "provider_status": 403,
                    },
                    {
                        "card_code": "CARD-500",
                        "status": "failed",
                        "provider_status": 500,
                        "message": "temporary upstream failure",
                    },
                ],
            },
            submitted_card_codes=["CARD-403", "CARD-500"],
        )

        self.assertEqual(summary["outcome"], "partial")
        self.assertEqual(summary["reclaim_summary"]["unreclaimable"], 1)
        self.assertEqual(summary["reclaim_summary"]["failed"], 1)
        self.assertEqual(summary["retryable_card_codes"], ["CARD-500"])
        self.assertTrue(summary["retry_available"])
        failures = {item["card_code"]: item for item in summary["reclaim_failures"]}
        self.assertFalse(failures["CARD-403"]["retryable"])
        self.assertEqual(failures["CARD-403"]["provider_status"], 403)

    def test_reclaim_summary_treats_no_action_and_excluded_download_as_recovered(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "done": 2,
                "all_tasks": [
                    {"card_code": "CARD-1", "status": "done", "no_action": True},
                    {"card_code": "CARD-2", "status": "done", "order_no": "ORDER-2"},
                ],
            },
            submitted_card_codes=["CARD-1", "CARD-2"],
            exclude_order_nos=["ORDER-2"],
        )

        self.assertEqual(summary["outcome"], "recovered")
        self.assertEqual(summary["reclaim_summary"]["download_failed"], 0)
        self.assertEqual(summary["reclaim_summary"]["download_skipped"], 1)
        self.assertEqual(summary["retryable_card_codes"], [])

    def test_reclaim_summary_does_not_guess_mixed_aggregate_retry_codes(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "failed": 1,
                "unreclaimable": 1,
            },
            submitted_card_codes=["CARD-403", "CARD-500"],
        )

        self.assertEqual(summary["outcome"], "partial")
        self.assertEqual(summary["retryable_card_codes"], [])
        self.assertFalse(summary["retry_available"])

    def test_reclaim_summary_keeps_submitted_codes_for_transport_retry(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {"ok": False, "error": "gateway timeout"},
            submitted_card_codes=["CARD-1", "CARD-2"],
        )

        self.assertEqual(summary["outcome"], "error")
        self.assertEqual(summary["retryable_card_codes"], ["CARD-1", "CARD-2"])
        self.assertTrue(summary["retry_available"])

    def test_reclaim_summary_adds_details_for_aggregate_only_failures(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "failed": 1,
                "unreclaimable": 1,
                "error": "provider rejected one task",
            },
            submitted_card_codes=["CARD-403", "CARD-500"],
        )

        self.assertEqual(len(summary["reclaim_failures"]), 2)
        self.assertEqual(
            {item["category"] for item in summary["reclaim_failures"]},
            {"retryable", "unrecoverable"},
        )
        self.assertFalse(summary["retry_available"])

    def test_reclaim_summary_does_not_duplicate_permanent_task_detail(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "failed": 1,
                "unreclaimable": 1,
                "all_tasks": [{
                    "card_code": "CARD-403",
                    "status": "failed",
                    "permanent": True,
                    "provider_status": 403,
                    "error_code": "account_deactivated",
                }],
            },
            submitted_card_codes=["CARD-403"],
        )

        self.assertEqual(len(summary["reclaim_failures"]), 1)
        self.assertEqual(summary["reclaim_failures"][0]["failure_bucket"], "unreclaimable")
        self.assertFalse(summary["retry_available"])

    def test_reclaim_summary_accepts_completed_status_and_payload_count(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "all_tasks": [{
                    "card_code": "CARD-1",
                    "status": "completed",
                    "order_no": "ORDER-1",
                    "download_token": "TOKEN-1",
                }],
            },
            submitted_card_codes=["CARD-1"],
            downloaded_payloads=[{"task": {"order_no": "ORDER-1"}, "data": {"accounts": [{"id": 1}]}}],
        )

        self.assertEqual(summary["outcome"], "recovered")
        self.assertEqual(summary["reclaim_summary"]["done"], 1)
        self.assertEqual(summary["downloaded"], 1)

    def test_reclaim_summary_excludes_permanent_done_tasks_from_download_expectations(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                # These aggregate values are intentionally stale and include
                # the permanent task; task categories must take precedence.
                "downloaded": 99,
                "download_failed": 99,
                "all_tasks": [
                    {
                        "card_code": "CARD-403",
                        "status": "completed",
                        "permanent": True,
                        "provider_status": 403,
                        "order_no": "ORDER-403",
                        "download_token": "TOKEN-403",
                    },
                    {
                        "card_code": "CARD-200",
                        "status": "completed",
                        "order_no": "ORDER-200",
                        "download_token": "TOKEN-200",
                    },
                ],
            },
            submitted_card_codes=["CARD-403", "CARD-200"],
            downloaded_payloads=[
                {"task": {"order_no": "ORDER-200"}, "data": {"accounts": [{"id": 2}]}}
            ],
        )

        self.assertEqual(summary["reclaim_summary"]["done"], 1)
        self.assertEqual(summary["reclaim_summary"]["unreclaimable"], 1)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["download_failed"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["outcome"], "partial")
        permanent_failure = next(
            item for item in summary["reclaim_failures"] if item["card_code"] == "CARD-403"
        )
        self.assertIn("HTTP 403", permanent_failure["reason"])
        self.assertEqual(permanent_failure["download_error"], "")

    def test_reclaim_summary_uses_payload_count_and_task_download_failures(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "downloaded": 7,
                "download_failed": 7,
                "all_tasks": [{
                    "card_code": "CARD-1",
                    "status": "done",
                    "order_no": "ORDER-1",
                    "download_token": "TOKEN-1",
                }],
            },
            submitted_card_codes=["CARD-1"],
            downloaded_payloads=[],
        )

        self.assertEqual(summary["downloaded"], 0)
        self.assertEqual(summary["download_failed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["retryable_card_codes"], ["CARD-1"])

    def test_reclaim_summary_partitions_explicit_permanent_and_retry_codes(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "failed": 2,
                "retryable_card_codes": ["CARD-500"],
                "permanent_card_codes": ["CARD-403"],
            },
            submitted_card_codes=["CARD-403", "CARD-500"],
        )

        self.assertEqual(summary["reclaim_summary"]["unreclaimable"], 1)
        self.assertEqual(summary["reclaim_summary"]["failed"], 1)
        self.assertEqual(summary["retryable_card_codes"], ["CARD-500"])
        self.assertEqual(summary["permanent_card_codes"], ["CARD-403"])
        self.assertEqual(
            {(item["card_code"], item["category"]) for item in summary["reclaim_failures"]},
            {("CARD-403", "unrecoverable"), ("CARD-500", "retryable")},
        )

    def test_explicit_permanent_code_overrides_stale_task_status(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "permanent_card_codes": ["CARD-403"],
                "all_tasks": [{
                    "card_code": "CARD-403",
                    "status": "queued",
                }],
            },
            submitted_card_codes=["CARD-403"],
        )

        self.assertEqual(summary["outcome"], "unrecoverable")
        self.assertEqual(summary["reclaim_summary"]["active"], 0)
        self.assertEqual(summary["retryable_card_codes"], [])
        self.assertEqual(summary["permanent_card_codes"], ["CARD-403"])

    def test_reclaim_summary_reads_reason_as_permanent_marker(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "all_tasks": [{
                    "card_code": "CARD-DISABLED",
                    "status": "failed",
                    "reason": "account_deactivated",
                }],
            },
            submitted_card_codes=["CARD-DISABLED"],
        )

        self.assertEqual(summary["reclaim_summary"]["unreclaimable"], 1)
        self.assertEqual(summary["retryable_card_codes"], [])
        self.assertEqual(summary["reclaim_failures"][0]["category"], "unrecoverable")

    def test_reclaim_summary_counts_explicit_permanent_buckets_on_task_details(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "not_owned_card_codes": ["CARD-NOT-OWNED"],
                "skipped_card_codes": ["CARD-SKIPPED"],
                "all_tasks": [
                    {"card_code": "CARD-NOT-OWNED", "status": "failed"},
                    {"card_code": "CARD-SKIPPED", "status": "failed"},
                ],
            },
            submitted_card_codes=["CARD-NOT-OWNED", "CARD-SKIPPED"],
        )

        self.assertEqual(summary["reclaim_summary"]["unreclaimable"], 0)
        self.assertEqual(summary["reclaim_summary"]["not_owned"], 1)
        self.assertEqual(summary["reclaim_summary"]["skipped"], 1)
        self.assertEqual(
            {item["failure_bucket"] for item in summary["reclaim_failures"]},
            {"not_owned", "skipped"},
        )

    def test_task_bucket_status_wins_over_generic_permanent_code_list(self) -> None:
        summary = reclaim.summarize_reclaim_result(
            {
                "ok": True,
                "permanent_card_codes": ["CARD-NOT-OWNED"],
                "all_tasks": [{
                    "card_code": "CARD-NOT-OWNED",
                    "status": "not_owned",
                }],
            },
            submitted_card_codes=["CARD-NOT-OWNED"],
        )

        self.assertEqual(summary["reclaim_summary"]["unreclaimable"], 0)
        self.assertEqual(summary["reclaim_summary"]["not_owned"], 1)
        self.assertEqual(summary["reclaim_failures"][0]["failure_bucket"], "not_owned")

    def test_download_payloads_skips_permanent_completed_tasks(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = []

            def download(self, order_no, token):
                self.calls.append((order_no, token))
                return b'{"accounts":[{"id":1}]}'

        client = FakeClient()
        payloads = reclaim.download_payloads(
            {
                "all_tasks": [{
                    "card_code": "CARD-403",
                    "status": "completed",
                    "permanent": True,
                    "provider_status": 403,
                    "order_no": "ORDER-403",
                    "download_token": "TOKEN-403",
                }]
            },
            client,
            max_payload_bytes=1024,
        )

        self.assertEqual(payloads, [])
        self.assertEqual(client.calls, [])

    def test_download_payloads_honors_provider_permanent_code_list(self) -> None:
        class FakeClient:
            def download(self, order_no, token):
                raise AssertionError("permanent code must not be downloaded")

        payloads = reclaim.download_payloads(
            {
                "permanent_card_codes": ["CARD-403"],
                "all_tasks": [{
                    "card_code": "CARD-403",
                    "status": "done",
                    "order_no": "ORDER-403",
                    "download_token": "TOKEN-403",
                }],
            },
            FakeClient(),
            max_payload_bytes=1024,
        )

        self.assertEqual(payloads, [])

    def test_download_payloads_honors_specific_permanent_code_lists(self) -> None:
        class FakeClient:
            def download(self, order_no, token):
                raise AssertionError("specific permanent code must not be downloaded")

        for field in ("not_owned_card_codes", "skipped_card_codes"):
            with self.subTest(field=field):
                payloads = reclaim.download_payloads(
                    {
                        field: ["CARD-PERM"],
                        "all_tasks": [{
                            "card_code": "CARD-PERM",
                            "status": "done",
                            "order_no": "ORDER-PERM",
                            "download_token": "TOKEN-PERM",
                        }],
                    },
                    FakeClient(),
                    max_payload_bytes=1024,
                )
                self.assertEqual(payloads, [])

    def test_refresh_reclaim_normalizes_malformed_provider_result(self) -> None:
        class FakeClient:
            def refresh_progress(self, card_codes):
                return ["malformed"]

        result = reclaim.refresh_reclaim(
            ["CARD-1"],
            redeem_client_factory=FakeClient,
            to_json=lambda value: value,
            max_payload_bytes=1024,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], {"ok": False, "error": "401 进度返回格式无效"})
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["retryable_card_codes"], ["CARD-1"])

    def test_refresh_reclaim_rejects_empty_provider_result(self) -> None:
        class FakeClient:
            def refresh_progress(self, card_codes):
                return {}

        result = reclaim.refresh_reclaim(
            ["CARD-1"],
            redeem_client_factory=FakeClient,
            to_json=lambda value: value,
            max_payload_bytes=1024,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["result"]["ok"], False)
        self.assertTrue(result["retry_available"])

    def test_automation_does_not_requeue_permanent_failure(self) -> None:
        stored = []
        import_calls = []
        result = automation.run_cycle(
            settings_loader=lambda: {
                "enabled": True,
                "auto_import": True,
                "proxy_id": 7,
                "group_ids": [3],
                "codex_fingerprint_mode": "device",
            },
            state_loader=lambda: {"pending_card_codes": [], "imported_order_nos": [], "run_history": []},
            reclaim_accounts=lambda **kwargs: {
                "ok": True,
                "reclaim_card_codes": ["CARD-403"],
                "downloaded_payloads": [],
                "result": {
                    "ok": True,
                    "failed": 1,
                    "all_tasks": [{
                        "card_code": "CARD-403",
                        "status": "failed",
                        "permanent": True,
                        "error_code": "account_deactivated",
                        "provider_status": 403,
                    }],
                },
            },
            refresh_reclaim=lambda *args, **kwargs: self.fail("initial scan should be used"),
            import_payload=lambda *args, **kwargs: import_calls.append(args),
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["outcome"], "unrecoverable")
        self.assertEqual(result["result"]["retryable_card_codes"], [])
        self.assertFalse(result["result"]["retry_available"])
        self.assertEqual(stored[-1]["pending_card_codes"], [])
        self.assertEqual(import_calls, [])

    def test_automation_transport_failure_keeps_retry_context_and_import_fields(self) -> None:
        stored = []
        result = automation.run_cycle(
            settings_loader=lambda: {"enabled": True, "auto_import": True},
            state_loader=lambda: {"pending_card_codes": [], "imported_order_nos": [], "run_history": []},
            reclaim_accounts=lambda **kwargs: {
                "ok": False,
                "reclaim_card_codes": ["CARD-1", "CARD-2"],
                "downloaded_payloads": [],
                "result": {"ok": False, "error": "gateway timeout"},
            },
            refresh_reclaim=lambda *args, **kwargs: self.fail("initial scan should be used"),
            import_payload=lambda *args, **kwargs: self.fail("transport failure must not import"),
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["result"]["retryable_card_codes"], ["CARD-1", "CARD-2"])
        self.assertTrue(result["result"]["retry_available"])
        self.assertEqual(result["result"]["import_status"], "not_attempted")
        self.assertEqual(result["result"]["import_error"], "")
        self.assertEqual(stored[-1]["pending_card_codes"], ["CARD-1", "CARD-2"])

    def test_automation_transport_mixed_permanent_failure_does_not_requeue_all_codes(self) -> None:
        stored = []
        result = automation.run_cycle(
            settings_loader=lambda: {"enabled": True, "auto_import": True},
            state_loader=lambda: {"pending_card_codes": [], "imported_order_nos": [], "run_history": []},
            reclaim_accounts=lambda **kwargs: {
                "ok": False,
                "reclaim_card_codes": ["CARD-403", "CARD-500"],
                "downloaded_payloads": [],
                "result": {
                    "ok": False,
                    "error": "partial provider response",
                    "failed": 1,
                    "unreclaimable": 1,
                },
            },
            refresh_reclaim=lambda *args, **kwargs: self.fail("initial scan should be used"),
            import_payload=lambda *args, **kwargs: self.fail("transport failure must not import"),
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["result"]["retryable_card_codes"], [])
        self.assertFalse(result["result"]["retry_available"])
        self.assertEqual(stored[-1]["pending_card_codes"], [])

    def test_automation_active_queue_excludes_permanent_task_codes(self) -> None:
        stored = []
        result = automation.run_cycle(
            settings_loader=lambda: {"enabled": True, "auto_import": False},
            state_loader=lambda: {"pending_card_codes": [], "imported_order_nos": [], "run_history": []},
            reclaim_accounts=lambda **kwargs: {
                "ok": True,
                "reclaim_card_codes": ["CARD-ACTIVE", "CARD-403"],
                "downloaded_payloads": [],
                "result": {
                    "ok": True,
                    "queued": 1,
                    "unreclaimable": 1,
                    "all_tasks": [
                        {"card_code": "CARD-ACTIVE", "status": "queued"},
                        {"card_code": "CARD-403", "status": "failed", "provider_status": 403},
                    ],
                },
            },
            refresh_reclaim=lambda *args, **kwargs: self.fail("initial scan should be used"),
            import_payload=lambda *args, **kwargs: self.fail("auto import is disabled"),
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        self.assertEqual(result["result"]["outcome"], "pending")
        self.assertEqual(stored[-1]["pending_card_codes"], ["CARD-ACTIVE"])
        self.assertNotIn("CARD-403", stored[-1]["pending_card_codes"])

    def test_automation_active_fallback_does_not_requeue_not_owned_codes(self) -> None:
        stored = []
        automation.run_cycle(
            settings_loader=lambda: {"enabled": True, "auto_import": False},
            state_loader=lambda: {"pending_card_codes": [], "imported_order_nos": [], "run_history": []},
            reclaim_accounts=lambda **kwargs: {
                "ok": True,
                "reclaim_card_codes": ["CARD-UNKNOWN", "CARD-NOT-OWNED"],
                "downloaded_payloads": [],
                "result": {
                    "ok": True,
                    "active": 1,
                    "not_owned": 1,
                },
            },
            refresh_reclaim=lambda *args, **kwargs: self.fail("initial scan should be used"),
            import_payload=lambda *args, **kwargs: self.fail("auto import is disabled"),
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        self.assertEqual(stored[-1]["pending_card_codes"], [])

    def test_automation_filters_conflicting_raw_retryable_and_permanent_codes(self) -> None:
        stored = []
        result = automation.run_cycle(
            settings_loader=lambda: {"enabled": True, "auto_import": False},
            state_loader=lambda: {"pending_card_codes": [], "imported_order_nos": [], "run_history": []},
            reclaim_accounts=lambda **kwargs: {
                "ok": True,
                "reclaim_card_codes": ["CARD-PERM", "CARD-RETRY"],
                "retryable_card_codes": ["CARD-PERM", "CARD-RETRY"],
                "permanent_card_codes": ["CARD-PERM"],
                "downloaded_payloads": [],
                "result": {"ok": True, "failed": 2},
            },
            refresh_reclaim=lambda *args, **kwargs: self.fail("initial scan should be used"),
            import_payload=lambda *args, **kwargs: self.fail("auto import is disabled"),
            store_state=stored.append,
            now=lambda: "2026-08-30T08:00:00+00:00",
        )

        self.assertEqual(result["result"]["retryable_card_codes"], ["CARD-RETRY"])
        self.assertEqual(stored[-1]["pending_card_codes"], ["CARD-RETRY"])

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

    def test_reclaim_retry_route_validates_codes_and_dispatches_explicit_retry(self) -> None:
        responses = []
        persisted = []
        common = {
            "send_json": lambda payload, status=200: responses.append((status, payload)),
            "test_connection": lambda: {},
            "reclaim_accounts": lambda **kwargs: {},
            "refresh_reclaim": lambda *args, **kwargs: {},
            "run_automation": lambda: {},
            "import_payload": lambda *args, **kwargs: {},
        }
        self.assertTrue(routes.handle_post(
            "/api/sub2api/reclaim-401/retry",
            {"card_codes": ["CARD-1", "CARD-1"], "exclude_order_nos": ["ORDER-1"]},
            **common,
            retry_reclaim=lambda codes, **kwargs: {"ok": True, "codes": codes, "kwargs": kwargs},
        ))
        self.assertEqual(responses[-1], (200, {
            "ok": True,
            "codes": ["CARD-1", "CARD-1"],
            "kwargs": {"exclude_order_nos": ["ORDER-1"]},
        }))
        self.assertTrue(routes.handle_post(
            "/api/sub2api/reclaim-401/retry",
            {"card_codes": ["CARD-2"], "persist_automation": True},
            **common,
            retry_reclaim=lambda codes, **kwargs: {"ok": True, "codes": codes},
            persist_automation_retry=lambda result: persisted.append(result) or {**result, "persisted": True},
        ))
        self.assertEqual(persisted, [{"ok": True, "codes": ["CARD-2"]}])
        self.assertEqual(responses[-1], (200, {"ok": True, "codes": ["CARD-2"], "persisted": True}))
        self.assertTrue(routes.handle_post(
            "/api/sub2api/reclaim401/retry",
            {"card_codes": []},
            **common,
            retry_reclaim=lambda codes, **kwargs: self.fail("empty retry must be rejected"),
        ))
        self.assertEqual(responses[-1][0], 400)

        self.assertTrue(routes.handle_post(
            "/api/sub2api/reclaim-progress",
            {"card_codes": []},
            **common,
        ))
        self.assertEqual(responses[-1][0], 400)

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

        self.assertTrue(routes.handle_post(
            "/api/sub2api/card-import-records/7/retry",
            {},
            **common_write,
            card_import_history_retry=lambda record_id: {"ok": True, "record_id": record_id},
        ))
        self.assertEqual(responses[-1], (200, {"ok": True, "record_id": 7}))

        self.assertTrue(routes.handle_post(
            "/api/sub2api/card-import-records/7/retry",
            {},
            **common_write,
            card_import_history_retry=lambda record_id: (_ for _ in ()).throw(
                card_import_history.RetryFailed("upstream unavailable")
            ),
        ))
        self.assertEqual(responses[-1], (502, {"detail": "upstream unavailable"}))

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
        self.assertTrue(routes.handle_post(
            "/api/sub2api/card-import-records/delete",
            {"id": 7},
            **common_write,
            card_import_history_deleter=lambda record_id: {
                "ok": True, "deleted_count": 1, "deleted_ids": [record_id],
            },
        ))
        self.assertEqual(responses[-1][1]["deleted_ids"], [7])
        self.assertTrue(routes.handle_post(
            "/api/sub2api/card-import-records/batch-delete",
            {"ids": [7, 8]},
            **common_write,
            card_import_history_batch_deleter=lambda record_ids: {
                "ok": True, "deleted_count": len(record_ids), "deleted_ids": record_ids,
            },
        ))
        self.assertEqual(responses[-1][1]["deleted_ids"], [7, 8])
        self.assertTrue(routes.handle_delete(
            "/api/sub2api/card-import-records/7",
            send_json=common_get["send_json"],
            card_import_history_deleter=lambda record_id: {
                "ok": True, "deleted_count": 1, "deleted_ids": [record_id],
            },
        ))
        self.assertEqual(responses[-1][1]["deleted_ids"], [7])
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
