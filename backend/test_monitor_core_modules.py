from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
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

    def test_browser_request_uses_storefront_visitor_id_and_detects_status_only_waf(self) -> None:
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

        class FakeDriver:
            script = ""

            def set_script_timeout(self, _seconds):
                return None

            def execute_async_script(self, script, _payload):
                self.script = script
                return {"status": 403, "content_type": "text/html", "text": "<html>challenge</html>"}

        driver = FakeDriver()
        with self.assertRaises(RuntimeError):
            manager._browser_request(driver, {"token": "SHOP", "current": 1})
        self.assertIn("Visitorid", driver.script)

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
        # A solved Aliyun page can retain the challenge marker in its source;
        # completion must trust a fresh catalog request instead.
        driver.page_source = "<div id='aliyunCaptcha'></div><main>verified</main>"
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

    def test_delete_shops_cleans_children_before_parent_for_legacy_schema(self) -> None:
        legacy_path = Path(self.directory.name) / "legacy-shop-delete.db"

        @contextmanager
        def legacy_database() -> sqlite3.Connection:
            connection = sqlite3.connect(legacy_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

        with legacy_database() as connection:
            connection.executescript(
                """
                CREATE TABLE watches (id INTEGER PRIMARY KEY, url TEXT NOT NULL);
                CREATE TABLE shops (id INTEGER PRIMARY KEY, url TEXT NOT NULL, token TEXT NOT NULL);
                CREATE TABLE shop_products (
                    shop_id INTEGER NOT NULL,
                    goods_key TEXT NOT NULL,
                    watch_id INTEGER NOT NULL,
                    listed INTEGER NOT NULL DEFAULT 1,
                    last_seen TEXT NOT NULL,
                    PRIMARY KEY (shop_id, goods_key),
                    FOREIGN KEY (shop_id) REFERENCES shops(id),
                    FOREIGN KEY (watch_id) REFERENCES watches(id)
                );
                CREATE TABLE shop_exclusions (
                    shop_id INTEGER NOT NULL,
                    goods_key TEXT NOT NULL,
                    removed_at TEXT NOT NULL,
                    PRIMARY KEY (shop_id, goods_key),
                    FOREIGN KEY (shop_id) REFERENCES shops(id)
                );
                CREATE TABLE shop_runs (
                    id INTEGER PRIMARY KEY,
                    shop_id INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY (shop_id) REFERENCES shops(id)
                );
                """
            )
            watch_id = connection.execute(
                "INSERT INTO watches(url) VALUES(?)",
                ("https://pay.ldxp.cn/item/legacy-shop-product",),
            ).lastrowid
            shop_id = connection.execute(
                "INSERT INTO shops(url, token) VALUES(?, ?)",
                ("https://pay.ldxp.cn/shop/LEGACYDELETE", "LEGACYDELETE"),
            ).lastrowid
            connection.execute(
                "INSERT INTO shop_products(shop_id, goods_key, watch_id, last_seen) VALUES(?, ?, ?, ?)",
                (shop_id, "legacy-product", watch_id, "2026-08-30T08:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO shop_exclusions(shop_id, goods_key, removed_at) VALUES(?, ?, ?)",
                (shop_id, "legacy-product", "2026-08-30T08:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO shop_runs(shop_id, fetched_at, status) VALUES(?, ?, 'success')",
                (shop_id, "2026-08-30T08:00:00+00:00"),
            )

        service = InventoryService(
            database=legacy_database,
            now=lambda: "2026-08-30T08:00:00+00:00",
            fetch_goods=lambda url: {},
            fetch_shop_catalog=lambda url, **kwargs: [],
            commerce_tags=lambda item: [],
            is_unlisted_error=lambda value: False,
            sync_intervals=lambda connection, shop_id, interval: 0,
        )
        self.assertEqual(service.delete_shops([shop_id]), 1)

        with legacy_database() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shops").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shop_products").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shop_exclusions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shop_runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0], 1)

    def test_inventory_links_single_product_to_discovered_shop_and_reuses_it(self) -> None:
        product = {
            "goods_key": "single-product",
            "title": "单商品",
            "price": "3.00",
            "market_price": "",
            "stock": 4,
            "description": "",
            "specs": {"店铺": "发现店铺"},
            "image": "",
            "sale_status": "on_sale",
            "raw_data": {"name": "单商品"},
            "shop": {
                "token": "DISCOVERED",
                "url": "https://pay.ldxp.cn/shop/DISCOVERED",
                "name": "发现店铺",
                "category_id": 114049,
                "category_name": "分类",
                "goods_type": "card",
            },
        }
        service = InventoryService(
            database=self.database,
            now=lambda: "2026-08-30T08:00:00+00:00",
            fetch_goods=lambda url: {
                **product,
                "goods_key": "single-product-2" if str(url).endswith("single-product-2") else product["goods_key"],
            },
            fetch_shop_catalog=lambda url, **kwargs: [],
            commerce_tags=lambda item: [],
            is_unlisted_error=lambda value: False,
            sync_intervals=lambda connection, shop_id, interval: 0,
        )
        with self.database() as connection:
            first = connection.execute(
                "INSERT INTO watches(url, name, created_at) VALUES(?, ?, ?)",
                ("https://pay.ldxp.cn/item/single-product", "单商品", "2026-08-30T08:00:00+00:00"),
            ).lastrowid
            second = connection.execute(
                "INSERT INTO watches(url, name, created_at) VALUES(?, ?, ?)",
                ("https://pay.ldxp.cn/item/single-product-2", "单商品2", "2026-08-30T08:00:00+00:00"),
            ).lastrowid

        first_result = service.record_fetch(first)
        second_result = service.record_fetch(second)

        self.assertEqual(first_result["shop_discovery"]["status"], "linked")
        self.assertEqual(second_result["shop_discovery"]["shop"]["id"], first_result["shop_discovery"]["shop"]["id"])
        with self.database() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shops").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shop_products").fetchone()[0], 2)

    def test_inventory_does_not_fabricate_shop_without_identity(self) -> None:
        product = {
            "goods_key": "standalone-product",
            "title": "独立商品",
            "price": "3.00",
            "market_price": "",
            "stock": None,
            "description": "",
            "specs": {},
            "image": "",
            "sale_status": "on_sale",
            "raw_data": {},
            "shop": None,
        }
        service = InventoryService(
            database=self.database,
            now=lambda: "2026-08-30T08:00:00+00:00",
            fetch_goods=lambda url: dict(product),
            fetch_shop_catalog=lambda url, **kwargs: [],
            commerce_tags=lambda item: [],
            is_unlisted_error=lambda value: False,
            sync_intervals=lambda connection, shop_id, interval: 0,
        )
        with self.database() as connection:
            watch_id = connection.execute(
                "INSERT INTO watches(url, name, created_at) VALUES(?, ?, ?)",
                ("https://pay.ldxp.cn/item/standalone-product", "独立商品", "2026-08-30T08:00:00+00:00"),
            ).lastrowid

        result = service.record_fetch(watch_id)

        self.assertEqual(result["shop_discovery"], {"status": "unavailable", "shop": None})
        with self.database() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shops").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shop_products").fetchone()[0], 0)

    def test_list_watches_backfills_shop_from_legacy_snapshot(self) -> None:
        service = InventoryService(
            database=self.database,
            now=lambda: "2026-08-30T08:00:00+00:00",
            fetch_goods=lambda url: {},
            fetch_shop_catalog=lambda url, **kwargs: [],
            commerce_tags=lambda item: [],
            is_unlisted_error=lambda value: False,
            sync_intervals=lambda connection, shop_id, interval: 0,
            discover_shop=storefront.discover_goods_shop,
        )
        with self.database() as connection:
            watch_id = connection.execute(
                "INSERT INTO watches(url, name, created_at) VALUES(?, ?, ?)",
                ("https://pay.ldxp.cn/item/legacy-product", "旧商品", "2026-08-30T08:00:00+00:00"),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO snapshots(watch_id, title, price, specs, goods_key, raw_data, sale_status, fetched_at, status)
                VALUES(?, '旧商品', '1.00', '{}', 'legacy-product', ?, 'on_sale', ?, 'success')
                """,
                (
                    watch_id,
                    '{"goods_key":"legacy-product","goods_type":"card","category":{"id":1,"name":"分类"},"user":{"token":"LEGACYSHOP","nickname":"旧店铺","link":"https://pay.ldxp.cn/shop/LEGACYSHOP"}}',
                    "2026-08-30T08:00:00+00:00",
                ),
            )

        shops = service.list_shops()
        watches = service.list_watches()

        self.assertEqual(shops[0]["token"], "LEGACYSHOP")
        self.assertEqual(watches[0]["shops"], [{"id": 1, "name": "旧店铺", "token": "LEGACYSHOP"}])
        with self.database() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shops").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM shop_products").fetchone()[0], 1)

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
