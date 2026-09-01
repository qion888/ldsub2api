from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from monitor_core import database as database_module
from monitor_core import history, preorders, settings, storefront
from monitor_core.browser_verification import BrowserVerificationManager
from monitor_core.inventory import InventoryService
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

    def test_browser_verification_batches_shops_after_one_challenge(self) -> None:
        with self.database() as connection:
            for token in ("BATCHONE", "BATCHTWO"):
                connection.execute(
                    "INSERT INTO shops(url, token, name, goods_type, created_at) VALUES(?, ?, ?, 'card', ?)",
                    (f"https://pay.ldxp.cn/shop/{token}", token, token, "2026-08-30T08:00:00+00:00"),
                )
            shop_ids = [row[0] for row in connection.execute("SELECT id FROM shops ORDER BY id")]

        class FakeDriver:
            page_source = "<div id='challenge'></div>"

            def execute_script(self, *_args):
                return None

            def quit(self):
                return None

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
        driver = FakeDriver()
        manager._create_driver = lambda: (driver, "chrome")
        attempts = {shop_ids[0]: 0}

        def sync(_driver, shop_id):
            if shop_id == shop_ids[0] and attempts[shop_id] == 0:
                attempts[shop_id] += 1
                raise RuntimeError("WAF challenge")
            return {"product_count": 1}

        manager._sync_shop = sync
        first = manager.start_all(shop_ids)
        self.assertEqual(first["status"], "awaiting_verification")
        self.assertEqual(first["pending_shop_ids"], shop_ids)
        self.assertEqual(first["current_shop_id"], shop_ids[0])
        driver.page_source = "<main>verified</main>"
        completed = manager.complete_all()
        self.assertEqual(completed["status"], "success")
        self.assertEqual(completed["completed"], 2)
        self.assertEqual([entry["id"] for entry in completed["results"]], shop_ids)
        self.assertEqual(completed["browser"], "chrome")

    def test_browser_order_supports_linux_and_explicit_selection(self) -> None:
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
        with patch("monitor_core.browser_verification.sys.platform", "linux"), patch.dict("os.environ", {}, clear=True):
            self.assertEqual(manager._browser_order(), ["chrome", "chromium", "edge", "firefox"])
        with patch.dict("os.environ", {"LDXP_WAF_BROWSER": "firefox"}):
            self.assertEqual(manager._browser_order(), ["firefox"])

    def test_storefront_normalizes_catalog_without_application_globals(self) -> None:
        product = storefront.normalize_goods_list_item(
            {
                "goods_key": "module-test",
                "name": "模块商品",
                "real_price": 6.5,
                "stock": 3,
                "status": 1,
                "goods_type": "card",
                "extend": {"limit_count": 2},
            },
            "MODULESHOP",
        )

        self.assertEqual(product["price"], "6.50")
        self.assertEqual(product["stock"], 3)
        self.assertEqual(product["limit_count"], 2)
        self.assertEqual(storefront.parse_item_url(product["source_url"])[0], "module-test")

    def test_inventory_service_marks_removed_catalog_product_unlisted(self) -> None:
        service = InventoryService(
            database=self.database,
            now=lambda: "2026-08-30T08:00:00+00:00",
            fetch_goods=lambda url: {},
            fetch_shop_catalog=lambda url, **kwargs: [],
            commerce_tags=lambda item: [],
            is_unlisted_error=lambda value: False,
            sync_intervals=lambda connection, shop_id, interval: 0,
        )
        with self.database() as connection:
            watch = connection.execute(
                "INSERT INTO watches(url, name, created_at) VALUES(?, ?, ?)",
                ("https://pay.ldxp.cn/item/removed", "Removed", "2026-08-30T08:00:00+00:00"),
            )
            shop = connection.execute(
                "INSERT INTO shops(url, token, created_at) VALUES(?, ?, ?)",
                ("https://pay.ldxp.cn/shop/REMOVED", "REMOVED", "2026-08-30T08:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO snapshots(watch_id, title, price, stock, specs, raw_data, sale_status, fetched_at, status) "
                "VALUES(?, 'Removed', '1.00', '4', '{}', '{}', 'on_sale', ?, 'success')",
                (watch.lastrowid, "2026-08-30T08:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen) VALUES(?, ?, ?, 0, ?)",
                (shop.lastrowid, "removed", watch.lastrowid, "2026-08-30T08:00:00+00:00"),
            )
            product = service.effective_watch_product(connection, watch.lastrowid)

        self.assertEqual(product["sale_status"], "off_sale")
        self.assertIsNone(product["stock"])

    def test_preorder_module_rejects_disabled_request_before_storage(self) -> None:
        with self.assertRaisesRegex(ValueError, "启用自动预购"):
            preorders.create_preorders(
                {"enabled": False, "items": [{"watch_id": 1, "quantity": 1}]},
                database=self.database,
                settings_loader=lambda: {},
                interval_normalizer=lambda value, default: default,
                now=lambda: "2026-08-30T08:00:00+00:00",
                effective_product=lambda *args: None,
                list_loader=lambda: [],
            )


if __name__ == "__main__":
    unittest.main()
