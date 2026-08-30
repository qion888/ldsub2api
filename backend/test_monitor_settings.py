import sqlite3
import unittest

from monitor_settings import (
    MAX_INTERVAL,
    MonitorNotFound,
    normalize_interval,
    update_shop_monitoring,
    update_watch_monitoring,
)


class MonitorSettingsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            """
            CREATE TABLE shops (
                id INTEGER PRIMARY KEY,
                name TEXT,
                keywords TEXT,
                category_id INTEGER,
                goods_type TEXT,
                enabled INTEGER,
                interval_seconds INTEGER
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE watches (
                id INTEGER PRIMARY KEY,
                name TEXT,
                enabled INTEGER,
                interval_seconds INTEGER
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE shop_products (
                shop_id INTEGER,
                watch_id INTEGER,
                listed INTEGER
            )
            """
        )
        self.connection.execute(
            "INSERT INTO shops VALUES(1, '测试店铺', '', NULL, 'card', 1, 300)"
        )
        self.connection.executemany(
            "INSERT INTO watches VALUES(?, ?, 1, 60)",
            ((10, "关联商品"), (11, "已下架商品"), (12, "独立商品")),
        )
        self.connection.executemany(
            "INSERT INTO shop_products VALUES(1, ?, ?)",
            ((10, 1), (11, 0)),
        )

    def tearDown(self):
        self.connection.close()

    def test_shop_interval_updates_only_listed_products(self):
        result = update_shop_monitoring(
            self.connection,
            1,
            {
                "name": "测试店铺",
                "keywords": "",
                "category_id": "",
                "goods_type": "card",
                "enabled": True,
                "interval_seconds": 900,
            },
        )

        intervals = dict(self.connection.execute("SELECT id, interval_seconds FROM watches"))
        self.assertEqual(result["interval_seconds"], 900)
        self.assertEqual(result["synced_product_count"], 1)
        self.assertEqual(intervals, {10: 900, 11: 60, 12: 60})

    def test_watch_settings_return_normalized_state(self):
        result = update_watch_monitoring(
            self.connection,
            12,
            {"name": "独立商品", "enabled": False, "interval_seconds": MAX_INTERVAL + 1},
        )

        self.assertFalse(result["enabled"])
        self.assertEqual(result["interval_seconds"], MAX_INTERVAL)

    def test_invalid_or_missing_monitor_is_rejected(self):
        self.assertEqual(normalize_interval(-10), 1)
        with self.assertRaises(ValueError):
            update_shop_monitoring(
                self.connection,
                1,
                {"name": "测试店铺", "goods_type": "bad type", "interval_seconds": 60},
            )
        with self.assertRaises(MonitorNotFound):
            update_watch_monitoring(self.connection, 999, {"interval_seconds": 60})


if __name__ == "__main__":
    unittest.main()
