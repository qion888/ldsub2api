from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monitor_core import database as database_module
from monitor_core import history, settings
from monitor_core.browser_verification import BrowserVerificationManager
from monitor_core.workers import MonitorWorker


class MonitorCoreModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "monitor.db"
        self.database = lambda: database_module.create_database(self.db_path)
        database_module.initialize_database(
            self.database,
            now=lambda: "2026-08-30T08:00:00+00:00",
            default_interval=300,
            default_redeem_url="https://redeem.example",
            default_sub2api_url="https://sub2api.example",
            default_automation={"enabled": False},
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_database_schema_and_settings_are_independent(self) -> None:
        settings.store_json(self.database, "feature", {"enabled": True})

        with self.database() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue({"watches", "snapshots", "shops", "preorders"}.issubset(tables))
        self.assertEqual(settings.read_json(self.database, "feature", {}), {"enabled": True})
        self.assertEqual(settings.normalize_service_url("https://example.test/", ""), "https://example.test")

    def test_history_calculates_price_and_stock_transitions(self) -> None:
        with self.database() as connection:
            watch = connection.execute(
                "INSERT INTO watches(url, name, created_at) VALUES(?, ?, ?)",
                ("https://pay.ldxp.cn/item/history", "History", "2026-08-30T08:00:00+00:00"),
            )
            for price, stock, fetched_at in (
                ("10.00", "0", "2026-08-30T08:00:00+00:00"),
                ("8.00", "4", "2026-08-30T08:01:00+00:00"),
                ("9.00", "0", "2026-08-30T08:02:00+00:00"),
            ):
                connection.execute(
                    "INSERT INTO snapshots(watch_id, price, stock, fetched_at, status) VALUES(?, ?, ?, ?, 'success')",
                    (watch.lastrowid, price, stock, fetched_at),
                )

        result = history.price_history(
            self.database,
            lambda value: f"{value:.2f}",
            watch.lastrowid,
            limit=25,
        )

        self.assertEqual(result["stats"]["price_change_count"], 2)
        self.assertEqual(result["stats"]["restock_count"], 1)
        self.assertEqual(result["stats"]["sold_out_count"], 1)
        self.assertEqual(result["stats"]["min_price"], "8.00")

    def test_worker_batches_failures_without_aborting_remaining_items(self) -> None:
        worker = MonitorWorker(
            database=self.database,
            record_inventory_fetch=lambda watch_id, cache=None: (
                (_ for _ in ()).throw(RuntimeError("failed"))
                if watch_id == 2
                else {"watch_id": watch_id}
            ),
            record_shop_fetch=lambda shop_id: {"shop_id": shop_id},
            process_preorder=lambda preorder_id, product: None,
            mark_preorder_check_error=lambda preorder_id, error: None,
            default_interval=300,
        )

        result = worker.fetch_many([1, 2, 3])

        self.assertEqual([item["ok"] for item in result], [True, False, True])
        self.assertEqual(result[1]["error"], "failed")

    def test_browser_verification_detects_configured_waf_markers(self) -> None:
        manager = BrowserVerificationManager(
            database=self.database,
            worker_lock=object(),
            record_shop_fetch=lambda *args, **kwargs: {},
            goods_list_rows=lambda payload: ([], {}),
            normalize_goods=lambda item, token: item,
            first_value=lambda item, keys: None,
            waf_error=RuntimeError,
            waf_markers=(b"aliyunCaptcha",),
            profile_path=Path(self.directory.name) / "profile",
        )

        self.assertTrue(manager._is_waf_html('<div id="aliyunCaptcha"></div>'))
        self.assertFalse(manager._is_waf_html("<main>products</main>"))


if __name__ == "__main__":
    unittest.main()
