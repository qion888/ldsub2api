import sqlite3
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import main


@contextmanager
def isolated_database(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class GoodsParserTests(unittest.TestCase):
    def request_api(self, method, path, payload):
        body = json.dumps(payload).encode("utf-8")
        handler = object.__new__(main.ApiHandler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        responses = []
        handler._send_json = lambda data, status=200: responses.append((status, data))
        getattr(handler, f"do_{method}")()
        return responses[-1]

    def seed_preorder_product(self, connection, stock=0, minimum=1, password_required=False):
        cursor = connection.execute(
            "INSERT INTO watches(url, name, enabled, interval_seconds, created_at) VALUES(?, ?, 1, 60, ?)",
            ("https://pay.ldxp.cn/item/preorder-test", "预购测试", main.utc_now()),
        )
        watch_id = cursor.lastrowid
        specs = {"最低起购": minimum, "查询密码": "需要" if password_required else "不需要"}
        connection.execute(
            """
            INSERT INTO snapshots(
                watch_id, title, price, stock, specs, sale_status, goods_key,
                raw_data, fetched_at, status
            ) VALUES(?, '预购测试', '2.50', ?, ?, 'on_sale', 'preorder-test', '{}', ?, 'success')
            """,
            (watch_id, None if stock is None else str(stock), json.dumps(specs, ensure_ascii=False), main.utc_now()),
        )
        shop = connection.execute(
            """
            INSERT INTO shops(url, token, name, goods_type, enabled, interval_seconds, created_at)
            VALUES('https://pay.ldxp.cn/shop/PREORDER', 'PREORDER', '预购店铺', 'card', 1, 300, ?)
            """,
            (main.utc_now(),),
        )
        connection.execute(
            "INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen) VALUES(?, 'preorder-test', ?, 1, ?)",
            (shop.lastrowid, watch_id, main.utc_now()),
        )
        connection.execute(
            """
            INSERT INTO settings(key, value) VALUES('checkout', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (json.dumps({"contact": "buyer@example.com", "note": "", "query_password": "saved-pass", "channel_id": 4}),),
        )
        return watch_id

    def seed_success_snapshot_followed_by_error(self, connection, error):
        cursor = connection.execute(
            "INSERT INTO watches(url, name, enabled, created_at) VALUES(?, ?, 1, ?)",
            ("https://pay.ldxp.cn/item/stale-state-test", "旧快照商品", main.utc_now()),
        )
        watch_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO snapshots(
                watch_id, title, price, stock, specs, sale_status, goods_key,
                raw_data, fetched_at, status
            ) VALUES(?, ?, '3.50', '7', ?, 'on_sale', 'stale-state-test', '{}', ?, 'success')
            """,
            (watch_id, "旧快照商品", json.dumps({"分类": "回归测试"}, ensure_ascii=False), main.utc_now()),
        )
        connection.execute(
            "INSERT INTO snapshots(watch_id, fetched_at, status, error) VALUES(?, ?, 'error', ?)",
            (watch_id, main.utc_now(), error),
        )
        return watch_id

    def test_fetch_buyer_juuid_follows_script_to_iframe(self):
        script = b"const iframe = document.createElement('iframe'); iframe.src = 'https://pay.ldxp.cn/shopApi/common/buyerBlackIframe';"
        iframe = b"<script> const juuid = 'ExampleJuuid123';window.parent.postMessage({type:'CloudBuyerBlack',juuid},'*');</script>"

        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit):
                return self.body

        with patch.object(main, "urlopen", side_effect=[FakeResponse(script), FakeResponse(iframe)]):
            identity = main.fetch_buyer_juuid("SHOPTEST")
        self.assertEqual(identity["juuid"], "ExampleJuuid123")
        self.assertIn("buyerBlackIframe", identity["iframe_url"])

    def test_shop_api_identifies_aliyun_waf_html(self):
        class FakeResponse:
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit):
                return b'<div id="aliyunCaptcha-sliding-slider"></div><script>var u_atoken="test"</script>'

        with patch.object(main, "urlopen", return_value=FakeResponse()):
            with self.assertRaises(main.WafChallengeRequired) as raised:
                main._post_shop_api("/shopApi/Shop/goodsList", {"token": "TEST"}, "https://pay.ldxp.cn/shop/TEST")
        self.assertIn("WAF", str(raised.exception))

    def test_create_official_payment_order_normalizes_payurl(self):
        response = {
            "code": 1,
            "data": {
                "trade_no": "LD260828TEST01",
                "total_amount": 1.99,
                "payurl": "https://pay.ldxp.cn/shopApi/Pay/payment?trade_no=LD260828TEST01",
            },
        }
        with patch.object(main, "_post_shop_api", return_value=response) as post:
            order = main.create_official_payment_order(
                goods_key="tp7o88",
                quantity=1,
                coupon_code="",
                channel_id=1,
                contact="buyer@example.test",
                query_password="test-query-pass",
                select_cards_ids=[],
                juuid="ExampleJuuid123",
                referer="https://pay.ldxp.cn/item/tp7o88",
            )
        self.assertEqual(order["trade_no"], "LD260828TEST01")
        self.assertEqual(order["amount"], "1.99")
        self.assertEqual(order["channel"], "alipay")
        endpoint, payload, referer = post.call_args.args[:3]
        self.assertEqual(endpoint, "/shopApi/Pay/order")
        self.assertEqual(payload["extend"]["juuid"], "ExampleJuuid123")
        self.assertEqual(referer, "https://pay.ldxp.cn/item/tp7o88")
        self.assertEqual(post.call_args.kwargs["visitor_id"].isalnum(), True)

    def test_fetch_payment_channels_keeps_enabled_upstream_ids(self):
        response = {
            "code": 1,
            "data": [
                {"id": 1, "show_name": "支付宝", "code": "AlipayPc", "status": 1},
                {"id": 4, "show_name": "微信", "code": "WeixinNative", "status": 1},
                {"id": 8, "show_name": "停用", "status": 0},
            ],
        }
        with patch.object(main, "_post_shop_api", return_value=response) as post:
            channels = main.fetch_payment_channels("SHOPTEST")
        self.assertEqual([channel["id"] for channel in channels], [1, 4])
        self.assertEqual(post.call_args.args[0], "/shopApi/Shop/getUserChannel")

    def test_shop_categories_normalize_names_ids_and_counts(self):
        response = {
            "code": 1,
            "data": [
                {"id": 157738, "name": "team", "goods_count": 2},
                {"id": "108401", "name": "codex官方直充", "goods_count": "4"},
                {"id": "bad", "name": "无效"},
                {"id": 157738, "name": "重复"},
            ],
        }
        with patch.object(main, "_post_shop_api", return_value=response) as post:
            result = main.fetch_shop_categories(
                "https://pay.ldxp.cn/shop/JVVH1Q5N", goods_type="card"
            )
        self.assertEqual(result["token"], "JVVH1Q5N")
        self.assertEqual(result["categories"], [
            {"id": 157738, "name": "team", "goods_count": 2},
            {"id": 108401, "name": "codex官方直充", "goods_count": 4},
        ])
        endpoint, payload, referer = post.call_args.args
        self.assertEqual(endpoint, "/shopApi/Shop/categoryList")
        self.assertEqual(payload, {"token": "JVVH1Q5N", "goods_type": "card", "category_key": ""})
        self.assertEqual(referer, "https://pay.ldxp.cn/shop/JVVH1Q5N")
        self.assertTrue(post.call_args.kwargs["visitor_id"])

    def test_shop_categories_accept_nested_data_and_empty_success(self):
        with patch.object(main, "_post_shop_api", return_value={
            "code": 1,
            "data": {"list": [{"category_id": "108401", "category_name": "codex官方直充", "count": "4"}]},
        }):
            result = main.fetch_shop_categories("https://pay.ldxp.cn/shop/JVVH1Q5N")
        self.assertEqual(result["categories"], [{"id": 108401, "name": "codex官方直充", "goods_count": 4}])

        with patch.object(main, "_post_shop_api", return_value={"code": 1, "data": None}):
            result = main.fetch_shop_categories("https://pay.ldxp.cn/shop/JVVH1Q5N")
        self.assertEqual(result["categories"], [])

    def test_shop_category_and_batch_delete_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = connection.execute(
                        "INSERT INTO watches(url, name, enabled) VALUES(?, '保留商品', 1)",
                        ("https://pay.ldxp.cn/item/batch-shop-keep",),
                    ).lastrowid
                    shop_ids = []
                    for token in ("BATCHA", "BATCHB", "BATCHC"):
                        shop_ids.append(connection.execute(
                            "INSERT INTO shops(url, token, name, goods_type, created_at) VALUES(?, ?, ?, 'card', ?)",
                            (f"https://pay.ldxp.cn/shop/{token}", token, token, main.utc_now()),
                        ).lastrowid)
                    connection.execute(
                        "INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen) VALUES(?, 'batch-shop-keep', ?, 1, ?)",
                        (shop_ids[0], watch_id, main.utc_now()),
                    )

                categories = {"token": "JVVH1Q5N", "url": "https://pay.ldxp.cn/shop/JVVH1Q5N", "goods_type": "card", "categories": [{"id": 108401, "name": "codex官方直充", "goods_count": 4}]}
                with patch.object(main, "fetch_shop_categories", return_value=categories):
                    category_status, category_result = self.request_api("POST", "/api/shops/categories", {
                        "url": "https://pay.ldxp.cn/shop/JVVH1Q5N", "goods_type": "card",
                    })
                self.assertEqual(category_status, 200)
                self.assertEqual(category_result["categories"][0]["name"], "codex官方直充")

                delete_status, delete_result = self.request_api(
                    "POST", "/api/shops/batch-delete", {"ids": shop_ids[:2]}
                )
                self.assertEqual(delete_status, 200)
                self.assertEqual(delete_result["deleted_count"], 2)
                with main.database() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM shops").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM watches WHERE id = ?", (watch_id,)).fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM shop_products").fetchone()[0], 0)

                invalid_status, _ = self.request_api("POST", "/api/shops/batch-delete", {"ids": []})
                self.assertEqual(invalid_status, 400)

    def test_monitor_interval_supports_one_second_and_clamps_bounds(self):
        self.assertEqual(main.normalize_interval(1), 1)
        self.assertEqual(main.normalize_interval("3"), 3)
        self.assertEqual(main.normalize_interval(0), main.DEFAULT_INTERVAL)
        self.assertEqual(main.normalize_interval(-10), 1)
        self.assertEqual(main.normalize_interval(999999), main.MAX_INTERVAL)

    def test_shop_monitor_route_syncs_linked_product_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_preorder_product(connection)
                    shop_id = connection.execute(
                        "SELECT id FROM shops WHERE token = 'PREORDER'"
                    ).fetchone()["id"]

                status, result = self.request_api(
                    "PUT",
                    f"/api/shops/{shop_id}",
                    {
                        "name": "预购店铺",
                        "keywords": "",
                        "category_id": None,
                        "goods_type": "card",
                        "enabled": True,
                        "interval_seconds": 900,
                    },
                )

                with main.database() as connection:
                    interval = connection.execute(
                        "SELECT interval_seconds FROM watches WHERE id = ?", (watch_id,)
                    ).fetchone()["interval_seconds"]
                self.assertEqual(status, 200)
                self.assertEqual(result["synced_product_count"], 1)
                self.assertEqual(result["interval_seconds"], 900)
                self.assertEqual(interval, 900)

    def test_accepts_only_canonical_ldxp_item_urls(self):
        key, url = main.parse_item_url("https://pay.ldxp.cn/item/tp7o88")
        self.assertEqual(key, "tp7o88")
        self.assertEqual(url, "https://pay.ldxp.cn/item/tp7o88")

        for invalid in (
            "http://pay.ldxp.cn/item/tp7o88",
            "https://example.com/item/tp7o88",
            "https://pay.ldxp.cn/shopApi/Shop/goodsInfo",
        ):
            with self.assertRaises(ValueError):
                main.parse_item_url(invalid)

    def test_normalizes_public_goods_fields(self):
        payload = {
            "code": 1,
            "data": {
                "goods_type": "card",
                "status": 1,
                "name": "测试商品",
                "price": 2,
                "real_price": 1.99,
                "market_price": 3,
                "description": "<p>说明 <strong>文本</strong></p>",
                "contact_format": "any",
                "coupon_status": 1,
                "category": {"name": "测试分类"},
                "user": {"nickname": "测试店铺"},
                "multipleoffers": {
                    "available": 1,
                    "discount_type": 1,
                    "rules": [{"condition": 10, "value": 8.8}],
                },
                "discount": {"available": 1, "rebate": 9.5},
                "fullgift": {
                    "available": 1,
                    "gift_type": 1,
                    "rules": [{"condition": 20, "value": 2}],
                },
                "extend": {"limit_count": 2, "query_password_status": 1},
            },
        }
        item = main.normalize_goods_payload(payload, "abc123")
        self.assertEqual(item["price"], "1.99")
        self.assertEqual(item["sale_status"], "on_sale")
        self.assertEqual(item["limit_count"], 2)
        self.assertTrue(item["query_password_required"])
        self.assertEqual(item["description"], "说明 文本")
        self.assertEqual(item["stock_label"], "接口未公开数量")
        tags = {tag["key"]: tag for tag in item["commerce_tags"]}
        self.assertEqual(tags["multiple_offers"]["detail"], "购10件享8.8折")
        self.assertEqual(tags["discount"]["detail"], "当前享9.5折")
        self.assertEqual(tags["full_gift"]["detail"], "购20件赠2件")
        self.assertEqual(tags["delivery"]["label"], "自动发货")
        self.assertEqual(tags["minimum"]["label"], "2件起购")
        self.assertIn("coupon", tags)
        self.assertIn("query_password", tags)

    def test_accepts_shop_urls_and_rejects_foreign_hosts(self):
        token, url = main.parse_shop_url("https://pay.ldxp.cn/shop/SHOPTEST")
        self.assertEqual(token, "SHOPTEST")
        self.assertEqual(url, "https://pay.ldxp.cn/shop/SHOPTEST")
        with self.assertRaises(ValueError):
            main.parse_shop_url("https://example.com/shop/SHOPTEST")

    def test_normalizes_store_inventory_fields(self):
        item = main.normalize_goods_list_item(
            {
                "goods_key": "tp7o88",
                "name": "店铺商品",
                "real_price": 1.99,
                "status": 1,
                "extend": {
                    "stock_count": 324,
                    "show_stock_type": 0,
                    "send_order": 0,
                    "limit_count": 1,
                    "query_password_status": 1,
                },
                "goods_type": "card",
                "category_name": "周限Team",
                "sales_count": 12,
            },
            "SHOPTEST",
        )
        self.assertEqual(item["stock"], 324)
        self.assertEqual(item["stock_label"], "324")
        self.assertEqual(item["sale_status"], "on_sale")
        self.assertEqual(item["specs"]["累计销量"], 12)
        self.assertEqual(item["limit_count"], 1)
        self.assertTrue(item["query_password_required"])
        tags = {tag["key"]: tag for tag in item["commerce_tags"]}
        self.assertEqual(tags["delivery"]["label"], "自动发货")
        self.assertEqual(tags["minimum"]["label"], "1件起购")

    def test_checkout_treats_limit_count_as_minimum_purchase(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    cursor = connection.execute(
                        "INSERT INTO watches(url, name, enabled) VALUES(?, ?, 1)",
                        ("https://pay.ldxp.cn/item/minimum-test", "起购测试"),
                    )
                    watch_id = cursor.lastrowid
                    connection.execute(
                        """
                        INSERT INTO snapshots(
                            watch_id, title, price, specs, sale_status, goods_key,
                            raw_data, fetched_at, status
                        ) VALUES(?, ?, '1.00', ?, 'on_sale', 'minimum-test', '{}', ?, 'success')
                        """,
                        (watch_id, "起购测试", json.dumps({"最低起购": 2}, ensure_ascii=False), main.utc_now()),
                    )

                rejected_status, rejected = self.request_api(
                    "POST", "/api/checkout/prepare", {"items": [{"watch_id": watch_id, "quantity": 1}]}
                )
                accepted_status, accepted = self.request_api(
                    "POST", "/api/checkout/prepare", {"items": [{"watch_id": watch_id, "quantity": 8}]}
                )

                self.assertEqual(rejected_status, 409)
                self.assertIn("最低 2 件起购", rejected["detail"])
                self.assertEqual(accepted_status, 200)
                self.assertEqual(accepted["items"][0]["quantity"], 8)

    def test_explicit_unlisted_error_overrides_stale_on_sale_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    self.seed_success_snapshot_followed_by_error(
                        connection, "商品未上架，如有疑问请联系商家"
                    )

                watch = main.list_watches()[0]

                self.assertEqual(watch["latest"]["sale_status"], "off_sale")
                self.assertIsNone(watch["latest"]["stock"])
                self.assertEqual(watch["latest"]["stock_label"], "未上架")
                self.assertEqual(watch["latest"]["title"], "旧快照商品")
                self.assertEqual(watch["latest"]["price"], "3.50")
                self.assertEqual(watch["last_attempt"]["status"], "error")
                self.assertEqual(watch["last_attempt"]["error"], "商品未上架，如有疑问请联系商家")

    def test_checkout_rejects_explicit_unlisted_error_after_success_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_success_snapshot_followed_by_error(
                        connection, "商品未上架，如有疑问请联系商家"
                    )

                status, result = self.request_api(
                    "POST", "/api/checkout/prepare", {"items": [{"watch_id": watch_id, "quantity": 1}]}
                )

                self.assertEqual(status, 409)
                self.assertRegex(result["detail"], "未上架|不是在售")

    def test_official_order_rejects_explicit_unlisted_error_without_upstream_call(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    self.seed_success_snapshot_followed_by_error(
                        connection, "商品未上架，如有疑问请联系商家"
                    )

                with patch.object(main, "create_official_payment_order") as create_order:
                    status, result = self.request_api(
                        "POST",
                        "/api/pay/order",
                        {
                            "items": [{"goods_key": "stale-state-test", "quantity": 1}],
                            "channel_id": 1,
                            "contact": "buyer@example.test",
                        },
                    )

                self.assertEqual(status, 400)
                self.assertRegex(result["detail"], "未上架|下架")
                create_order.assert_not_called()

    def test_preorder_rejects_explicit_unlisted_error_after_zero_stock_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_success_snapshot_followed_by_error(
                        connection, "商品未上架，如有疑问请联系商家"
                    )
                    connection.execute(
                        "UPDATE snapshots SET stock = '0' WHERE watch_id = ? AND status = 'success'",
                        (watch_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO settings(key, value) VALUES('checkout', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (json.dumps({"contact": "buyer@example.test", "channel_id": 1}),),
                    )

                with self.assertRaises(main.PreorderConflict) as raised:
                    main.create_preorders({
                        "enabled": True,
                        "interval_seconds": 1,
                        "items": [{"watch_id": watch_id, "quantity": 1}],
                    })

                self.assertIn("未上架", str(raised.exception))

    def test_removed_shop_product_blocks_checkout_and_official_order(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_success_snapshot_followed_by_error(connection, "网络连接超时")
                    shop = connection.execute(
                        """
                        INSERT INTO shops(url, token, name, goods_type, enabled, interval_seconds, created_at)
                        VALUES(?, ?, ?, 'card', 1, 300, ?)
                        """,
                        ("https://pay.ldxp.cn/shop/REMOVED", "REMOVED", "已移除店铺", main.utc_now()),
                    )
                    connection.execute(
                        """
                        INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen)
                        VALUES(?, 'stale-state-test', ?, 0, ?)
                        """,
                        (shop.lastrowid, watch_id, main.utc_now()),
                    )

                watch = main.list_watches()[0]
                self.assertEqual(watch["latest"]["sale_status"], "off_sale")
                self.assertIsNone(watch["latest"]["stock"])
                self.assertEqual(watch["latest"]["stock_label"], "未上架")

                checkout_status, checkout = self.request_api(
                    "POST", "/api/checkout/prepare", {"items": [{"watch_id": watch_id, "quantity": 1}]}
                )
                self.assertEqual(checkout_status, 409)
                self.assertIn("未上架", checkout["detail"])

                with patch.object(main, "create_official_payment_order") as create_order:
                    order_status, order = self.request_api(
                        "POST",
                        "/api/pay/order",
                        {
                            "items": [{"goods_key": "stale-state-test", "quantity": 1}],
                            "channel_id": 1,
                            "contact": "buyer@example.test",
                        },
                    )
                self.assertEqual(order_status, 400)
                self.assertIn("未上架", order["detail"])
                create_order.assert_not_called()

    def test_network_error_keeps_stale_success_snapshot_purchasable(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_success_snapshot_followed_by_error(connection, "网络连接超时")

                watch = main.list_watches()[0]
                self.assertEqual(watch["latest"]["sale_status"], "on_sale")
                self.assertEqual(watch["latest"]["stock"], 7)
                self.assertEqual(watch["latest"]["stock_label"], "7")
                self.assertEqual(watch["last_attempt"]["error"], "网络连接超时")

                checkout_status, checkout = self.request_api(
                    "POST", "/api/checkout/prepare", {"items": [{"watch_id": watch_id, "quantity": 1}]}
                )
                self.assertEqual(checkout_status, 200)
                self.assertEqual(checkout["items"][0]["goods_key"], "stale-state-test")

                with patch.object(
                    main,
                    "create_official_payment_order",
                    return_value={
                        "trade_no": "LD-NETWORK-ERROR",
                        "payment_url": "https://pay.ldxp.cn/pay/LD-NETWORK-ERROR",
                        "amount": "3.50",
                        "channel": "alipay",
                    },
                ) as create_order:
                    order_status, order = self.request_api(
                        "POST",
                        "/api/pay/order",
                        {
                            "items": [{"goods_key": "stale-state-test", "quantity": 1}],
                            "channel_id": 1,
                            "contact": "buyer@example.test",
                        },
                    )

                self.assertEqual(order_status, 200)
                self.assertEqual(order["trade_no"], "LD-NETWORK-ERROR")
                create_order.assert_called_once()

    def test_legacy_limit_label_is_exposed_as_minimum_purchase(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE snapshot(id INTEGER, specs TEXT, stock TEXT, raw_data TEXT)")
            row = connection.execute(
                "SELECT 1 AS id, ? AS specs, NULL AS stock, '{}' AS raw_data",
                (json.dumps({"单次限购": 3}, ensure_ascii=False),),
            ).fetchone()
            product = main.serialize_snapshot(row)
        finally:
            connection.close()

        self.assertNotIn("单次限购", product["specs"])
        self.assertEqual(product["specs"]["最低起购"], 3)
        self.assertEqual(product["limit_count"], 3)

    def test_checkout_settings_persist_password_and_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                status, saved = self.request_api(
                    "PUT", "/api/settings/checkout",
                    {"contact": "buyer@example.test", "note": "next", "query_password": "test-query-pass", "channel_id": 4},
                )
                with main.database() as connection:
                    row = connection.execute("SELECT value FROM settings WHERE key = 'checkout'").fetchone()

                self.assertEqual(status, 200)
                self.assertEqual(saved["channel_id"], 4)
                self.assertEqual(json.loads(row["value"]), saved)

    def test_checkout_settings_accepts_storage_mode_and_coupon(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                status, saved = self.request_api(
                    "PUT", "/api/settings/checkout",
                    {
                        "contact": "buyer@example.test",
                        "query_password": "secret",
                        "coupon_code": "WELCOME10",
                        "storage_mode": "browser",
                        "channel_id": 1,
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(saved["coupon_code"], "WELCOME10")
                self.assertEqual(saved["storage_mode"], "browser")
                self.assertEqual(main.checkout_settings()["storage_mode"], "browser")

    def test_preorder_requires_explicit_enable(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_preorder_product(connection)
                status, result = self.request_api(
                    "POST", "/api/preorders", {"items": [{"watch_id": watch_id, "quantity": 1}]}
                )
                self.assertEqual(status, 400)
                self.assertIn("勾选", result["detail"])

    def test_preorder_rejects_unknown_or_available_stock(self):
        for stock, expected in ((None, "库存数量未知"), (3, "当前不是缺货状态")):
            with self.subTest(stock=stock), tempfile.TemporaryDirectory() as directory:
                database_path = Path(directory) / "test.db"
                with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                    main.init_database()
                    with main.database() as connection:
                        watch_id = self.seed_preorder_product(connection, stock=stock)
                    status, result = self.request_api(
                        "POST", "/api/preorders",
                        {"enabled": True, "interval_seconds": 1, "items": [{"watch_id": watch_id, "quantity": 1}]},
                    )
                    self.assertEqual(status, 409)
                    self.assertIn(expected, result["detail"])

    def test_preorder_persists_quantity_interval_and_checkout_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_preorder_product(connection, stock=0, minimum=2, password_required=True)
                status, result = self.request_api(
                    "POST", "/api/preorders",
                    {"enabled": True, "interval_seconds": 3, "items": [{"watch_id": watch_id, "quantity": 5}]},
                )
                self.assertEqual(status, 201)
                self.assertEqual(result[0]["quantity"], 5)
                self.assertEqual(result[0]["interval_seconds"], 3)
                with main.database() as connection:
                    row = connection.execute("SELECT * FROM preorders WHERE watch_id = ?", (watch_id,)).fetchone()
                self.assertEqual(row["contact"], "buyer@example.com")
                self.assertEqual(row["query_password"], "saved-pass")
                self.assertEqual(row["channel_id"], 4)

    def test_preorder_enforces_minimum_purchase(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_preorder_product(connection, stock=0, minimum=4)
                status, result = self.request_api(
                    "POST", "/api/preorders",
                    {"enabled": True, "items": [{"watch_id": watch_id, "quantity": 2}]},
                )
                self.assertEqual(status, 409)
                self.assertIn("最低 4 件起购", result["detail"])

    def test_preorder_triggers_once_when_requested_stock_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_preorder_product(connection, stock=0)
                created = main.create_preorders({
                    "enabled": True,
                    "interval_seconds": 1,
                    "items": [{"watch_id": watch_id, "quantity": 2}],
                })
                preorder_id = created[0]["id"]
                product = {
                    "title": "预购测试", "stock": 1, "sale_status": "on_sale",
                    "goods_key": "preorder-test", "query_password_required": False,
                }
                with patch.object(main, "fetch_buyer_juuid", return_value={"juuid": "test-juuid"}), patch.object(
                    main,
                    "create_official_payment_order",
                    return_value={
                        "trade_no": "LD-PREORDER-1", "payment_url": "https://pay.ldxp.cn/pay/LD-PREORDER-1",
                        "amount": "5.00", "channel": "wechat",
                    },
                ) as create_order:
                    self.assertIsNone(main.process_preorder(preorder_id, product))
                    product["stock"] = 2
                    triggered = main.process_preorder(preorder_id, product)
                    duplicate = main.process_preorder(preorder_id, product)
                self.assertEqual(triggered["status"], "triggered")
                self.assertIsNone(duplicate)
                create_order.assert_called_once()
                self.assertEqual(create_order.call_args.kwargs["query_password"], "")
                with main.database() as connection:
                    row = connection.execute("SELECT * FROM preorders WHERE id = ?", (preorder_id,)).fetchone()
                self.assertEqual(row["status"], "triggered")
                self.assertEqual(row["enabled"], 0)
                self.assertEqual(row["trade_no"], "LD-PREORDER-1")
                self.assertEqual(row["payment_url"], "https://pay.ldxp.cn/pay/LD-PREORDER-1")

    def test_preorder_order_failure_disables_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_preorder_product(connection, stock=0)
                preorder_id = main.create_preorders({
                    "enabled": True, "items": [{"watch_id": watch_id, "quantity": 1}],
                })[0]["id"]
                product = {
                    "title": "预购测试", "stock": 1, "sale_status": "on_sale",
                    "goods_key": "preorder-test", "query_password_required": False,
                }
                with patch.object(main, "fetch_buyer_juuid", return_value={"juuid": "test-juuid"}), patch.object(
                    main, "create_official_payment_order", side_effect=RuntimeError("上游结果不确定")
                ) as create_order:
                    failed = main.process_preorder(preorder_id, product)
                    duplicate = main.process_preorder(preorder_id, product)
                self.assertEqual(failed["status"], "error")
                self.assertIsNone(duplicate)
                create_order.assert_called_once()
                with main.database() as connection:
                    row = connection.execute("SELECT status, enabled, last_error FROM preorders WHERE id = ?", (preorder_id,)).fetchone()
                self.assertEqual((row["status"], row["enabled"]), ("error", 0))
                self.assertIn("上游结果不确定", row["last_error"])

    def test_shop_catalog_uses_goods_list_parameters(self):
        response = {
            "code": 1,
            "data": {
                "list": [{"goods_key": "tp7o88", "name": "商品", "price": 1.99}],
                "total": 1,
            },
        }
        with patch.object(main, "_post_shop_api", return_value=response) as post:
            products = main.fetch_shop_catalog(
                "https://pay.ldxp.cn/shop/SHOPTEST",
                category_id=197663,
                goods_type="card",
            )
        self.assertEqual(len(products), 1)
        endpoint, payload, referer = post.call_args.args
        self.assertEqual(endpoint, "/shopApi/Shop/goodsList")
        self.assertEqual(payload["token"], "SHOPTEST")
        self.assertEqual(payload["category_id"], 197663)
        self.assertEqual(payload["goods_type"], "card")
        self.assertEqual(referer, "https://pay.ldxp.cn/shop/SHOPTEST")

        with patch.object(main, "_post_shop_api", return_value=response) as post_all:
            main.fetch_shop_catalog("https://pay.ldxp.cn/shop/SHOPTEST")
        self.assertEqual(post_all.call_args.args[1]["category_id"], "")

    def test_shop_refresh_imports_products_and_stock(self):
        product = main.normalize_goods_list_item(
            {
                "goods_key": "tp7o88",
                "name": "库存商品",
                "price": 1.99,
                "status": 1,
                "stock": 8,
                "user": {"nickname": "AI"},
            },
            "SHOPTEST",
        )
        database_uri = "file:shop-refresh-test?mode=memory&cache=shared"
        anchor = sqlite3.connect(database_uri, uri=True)

        @contextmanager
        def memory_database():
            connection = sqlite3.connect(database_uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        try:
            with patch.object(main, "database", side_effect=memory_database):
                main.init_database()
                with main.database() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO shops(
                            url, token, name, goods_type, enabled,
                            interval_seconds, created_at
                        ) VALUES(?, ?, ?, 'card', 1, 300, ?)
                        """,
                        (
                            "https://pay.ldxp.cn/shop/SHOPTEST",
                            "SHOPTEST",
                            "SHOPTEST",
                            main.utc_now(),
                        ),
                    )
                    shop_id = cursor.lastrowid
                with patch.object(main, "fetch_shop_catalog", return_value=[product]):
                    summary = main.record_shop_fetch(shop_id)
                self.assertEqual(summary["product_count"], 1)
                shop = main.list_shops()[0]
                self.assertEqual(shop["name"], "AI")
                self.assertEqual(shop["total_stock"], 8)
                self.assertEqual(shop["known_stock_count"], 1)
                watch = main.list_watches()[0]
                self.assertEqual(watch["latest"]["stock"], 8)
                self.assertEqual(watch["shops"][0]["token"], "SHOPTEST")
                self.assertEqual(watch["interval_seconds"], 300)
                self.assertEqual(main.delete_watches([watch["id"]]), 1)
                with patch.object(main, "fetch_shop_catalog", return_value=[product]):
                    summary = main.record_shop_fetch(shop_id)
                self.assertEqual(summary["product_count"], 0)
                self.assertEqual(main.list_watches(), [])
        finally:
            anchor.close()

    def test_child_product_refresh_uses_shop_inventory_and_updates_last_run(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = self.seed_preorder_product(connection, stock=None)
                product = main.normalize_goods_list_item({
                    "goods_key": "preorder-test",
                    "name": "店铺库存商品",
                    "price": 2.5,
                    "status": 1,
                    "stock": 12,
                }, "PREORDER")
                with patch.object(main, "fetch_shop_catalog", return_value=[product]) as fetch_shop, \
                     patch.object(main, "fetch_goods", side_effect=AssertionError("不应调用商品详情接口")):
                    refreshed = main.record_inventory_fetch(watch_id)

                self.assertEqual(refreshed["refresh_source"], "shop")
                self.assertEqual(refreshed["stock"], 12)
                self.assertEqual(refreshed["stock_label"], "12")
                fetch_shop.assert_called_once()
                with main.database() as connection:
                    watch = connection.execute("SELECT last_run FROM watches WHERE id = ?", (watch_id,)).fetchone()
                self.assertIsNotNone(watch["last_run"])

    def test_batch_inventory_refresh_reuses_one_shop_request(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    first_id = self.seed_preorder_product(connection, stock=0)
                    second = connection.execute(
                        "INSERT INTO watches(url, name, enabled, created_at) VALUES(?, ?, 0, ?)",
                        ("https://pay.ldxp.cn/item/second-test", "第二个商品", main.utc_now()),
                    )
                    second_id = second.lastrowid
                    shop_id = connection.execute("SELECT id FROM shops WHERE token = 'PREORDER'").fetchone()["id"]
                    connection.execute(
                        "INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen) VALUES(?, ?, ?, 1, ?)",
                        (shop_id, "second-test", second_id, main.utc_now()),
                    )
                products = [
                    main.normalize_goods_list_item({"goods_key": "preorder-test", "name": "第一", "stock": 2, "status": 1}, "PREORDER"),
                    main.normalize_goods_list_item({"goods_key": "second-test", "name": "第二", "stock": 7, "status": 1}, "PREORDER"),
                ]
                with patch.object(main, "fetch_shop_catalog", return_value=products) as fetch_shop:
                    results = main.WORKER.fetch_many([first_id, second_id])

                self.assertTrue(all(item["ok"] for item in results))
                self.assertEqual([item["data"]["stock"] for item in results], [2, 7])
                fetch_shop.assert_called_once()

    def test_inventory_refresh_route_uses_batch_worker(self):
        expected = [{"id": 4, "ok": True, "data": {"stock": 6, "refresh_source": "shop"}}]
        with patch.object(main.WORKER, "fetch_many", return_value=expected) as fetch_many:
            status, result = self.request_api("POST", "/api/watches/inventory-refresh", {"ids": [4, 4]})
        self.assertEqual(status, 200)
        self.assertEqual(result["results"], expected)
        fetch_many.assert_called_once_with([4])

    def test_sub2api_admin_key_uses_x_api_key_for_test_and_import(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                status, saved = self.request_api(
                    "PUT",
                    "/api/sub2api/config",
                    {"base_url": "https://sub2api.example", "admin_key": "secret-key"},
                )
                self.assertEqual(status, 200)
                self.assertTrue(saved["admin_key_set"])

                class FakeResponse:
                    def __init__(self, payload):
                        self.status = 200
                        self.payload = json.dumps(payload).encode("utf-8")

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False

                    def read(self, _limit):
                        return self.payload

                requests = []

                def fake_urlopen(request, timeout):
                    requests.append(request)
                    if request.full_url.endswith("/accounts"):
                        return FakeResponse({"data": {"accounts": []}})
                    if "/accounts?" in request.full_url:
                        items = [{
                            "id": 91,
                            "name": "导入账号 1",
                            "platform": "openai",
                            "type": "oauth",
                            "extra": {"codex_fingerprint_mode": "full"},
                        }] if "type=oauth" in request.full_url else []
                        return FakeResponse({"data": {"items": items, "pages": 1}})
                    return FakeResponse({"data": {"account_created": 1, "account_failed": 0}})

                with patch.object(main, "urlopen", side_effect=fake_urlopen):
                    test_status, test_result = self.request_api("POST", "/api/sub2api/test", {})
                    import_status, import_result = self.request_api(
                        "POST",
                        "/api/sub2api/import",
                        {
                            "data": {
                                "type": "sub2api-data",
                                "version": 1,
                                "accounts": [{
                                    "platform": "openai",
                                    "type": "oauth",
                                    "credentials": {"access_token": "token"},
                                }],
                                "proxies": [],
                            },
                            "codex_fingerprint_mode": "full",
                        },
                    )

                self.assertEqual(test_status, 200)
                self.assertTrue(test_result["ok"])
                self.assertEqual(import_status, 200)
                self.assertTrue(import_result["ok"])
                self.assertGreaterEqual(len(requests), 6)
                for request in requests:
                    headers = {key.lower(): value for key, value in request.header_items()}
                    self.assertEqual(headers.get("x-api-key"), "secret-key")
                    self.assertNotIn("authorization", headers)
                batch_request = next(request for request in requests if request.full_url.endswith("/accounts/data"))
                import_payload = json.loads(batch_request.data.decode("utf-8"))
                self.assertEqual(
                    import_payload["data"]["accounts"][0]["extra"]["codex_fingerprint_mode"],
                    "full",
                )
                self.assertEqual(import_result["fingerprint_verification"]["verified"], 1)
                self.assertEqual(import_result["fingerprint_verification"]["unresolved"], 0)

    def test_sub2api_options_and_assigned_import_use_existing_proxy_and_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                self.request_api(
                    "PUT",
                    "/api/sub2api/config",
                    {"base_url": "https://sub2api.example", "admin_key": "secret-key"},
                )

                class FakeResponse:
                    status = 200

                    def __init__(self, payload):
                        self.payload = json.dumps(payload).encode("utf-8")

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False

                    def read(self, _limit):
                        return self.payload

                batch_payloads = []

                def fake_urlopen(request, timeout):
                    headers = {key.lower(): value for key, value in request.header_items()}
                    self.assertEqual(headers.get("x-api-key"), "secret-key")
                    if "/proxies/all" in request.full_url:
                        return FakeResponse({
                            "data": [{
                                "id": 7, "name": "HK Proxy", "protocol": "socks5",
                                "host": "proxy.example", "port": 1080, "status": "active",
                                "password": "must-not-leak", "account_count": 2,
                            }]
                        })
                    if request.full_url.endswith("/groups/all"):
                        return FakeResponse({
                            "data": [{
                                "id": 3, "name": "Codex", "platform": "openai",
                                "status": "active", "account_count": 4,
                            }]
                        })
                    if request.full_url.endswith("/accounts/batch"):
                        batch_payloads.append(json.loads(request.data.decode("utf-8")))
                        return FakeResponse({"data": {"success": 1, "failed": 0, "results": [{"id": 101, "name": "Recovered account", "success": True}]}})
                    if "/accounts?" in request.full_url:
                        items = [{
                            "id": 101,
                            "name": "Recovered account",
                            "platform": "openai",
                            "type": "oauth",
                            "extra": {"source": "reclaim", "codex_fingerprint_mode": "session"},
                        }] if "type=oauth" in request.full_url else []
                        return FakeResponse({"data": {"items": items, "pages": 1}})
                    raise AssertionError(request.full_url)

                with patch.object(main, "urlopen", side_effect=fake_urlopen):
                    options_status, options = self.request_api("GET", "/api/sub2api/options", {})
                    import_status, imported = self.request_api(
                        "POST",
                        "/api/sub2api/import",
                        {
                            "assign_existing": True,
                            "proxy_id": 7,
                            "group_ids": [9, 3, 3],
                            "codex_fingerprint_mode": "session",
                            "data": {
                                "type": "sub2api-data",
                                "version": 1,
                                "accounts": [{
                                    "name": "Recovered account",
                                    "platform": "openai",
                                    "type": "oauth",
                                    "credentials": {"access_token": "token"},
                                    "extra": {"source": "reclaim"},
                                    "concurrency": 3,
                                    "priority": 5,
                                }],
                                "proxies": [{"password": "json-secret"}],
                            },
                        },
                    )

                self.assertEqual(options_status, 200)
                self.assertEqual(options["proxy_count"], 1)
                self.assertEqual(options["group_count"], 1)
                self.assertNotIn("password", options["proxies"][0])
                self.assertEqual(import_status, 200)
                self.assertEqual(imported["mode"], "assigned")
                self.assertEqual(imported["proxy_id"], 7)
                self.assertEqual(imported["group_ids"], [3, 9])
                self.assertEqual(len(batch_payloads), 1)
                account = batch_payloads[0]["accounts"][0]
                self.assertEqual(account["proxy_id"], 7)
                self.assertEqual(account["group_ids"], [3, 9])
                self.assertEqual(account["extra"]["source"], "reclaim")
                self.assertEqual(account["extra"]["codex_fingerprint_mode"], "session")
                self.assertEqual(imported["codex_fingerprint_mode"], "session")
                self.assertEqual(imported["codex_account_count"], 1)
                self.assertEqual(imported["fingerprint_verification"]["verified"], 1)
                self.assertEqual(imported["fingerprint_verification"]["unresolved"], 0)
                self.assertNotIn("proxies", batch_payloads[0])

    def test_sub2api_codex_fingerprint_mode_is_validated_and_scoped(self):
        source = {
            "type": "sub2api-data",
            "version": 1,
            "accounts": [
                {"platform": "openai", "type": "oauth", "credentials": {"access_token": "a"}, "extra": {"codex_fingerprint_mode": "full"}},
                {"platform": "openai", "type": "setup-token", "credentials": {"setup_token": "s"}},
                {"platform": "openai", "type": "apikey", "credentials": {"api_key": "b"}},
                {"platform": "anthropic", "type": "oauth", "credentials": {"access_token": "c"}},
            ],
            "proxies": [],
        }
        normalized = main._validate_sub2api_data(source)

        disabled, mode, account_count = main._sub2api_apply_codex_fingerprint_mode(normalized, "off")
        self.assertEqual(mode, "off")
        self.assertEqual(account_count, 2)
        self.assertNotIn("codex_fingerprint_mode", disabled["accounts"][0]["extra"])
        self.assertNotIn("codex_fingerprint_mode", disabled["accounts"][1].get("extra", {}))
        self.assertNotIn("codex_fingerprint_mode", disabled["accounts"][2].get("extra", {}))
        self.assertNotIn("codex_fingerprint_mode", disabled["accounts"][3].get("extra", {}))
        self.assertEqual(source["accounts"][0]["extra"]["codex_fingerprint_mode"], "full")

        converged, mode, account_count = main._sub2api_apply_codex_fingerprint_mode(normalized, "device")
        self.assertEqual(mode, "device")
        self.assertEqual(account_count, 2)
        self.assertEqual(converged["accounts"][0]["extra"]["codex_fingerprint_mode"], "device")
        self.assertEqual(converged["accounts"][1]["extra"]["codex_fingerprint_mode"], "device")
        self.assertNotIn("codex_fingerprint_mode", converged["accounts"][2].get("extra", {}))
        self.assertNotIn("codex_fingerprint_mode", converged["accounts"][3].get("extra", {}))

        with self.assertRaisesRegex(ValueError, "off、device、session 或 full"):
            main._sub2api_apply_codex_fingerprint_mode(normalized, "invalid")

    def test_sub2api_multiple_json_merge_and_fingerprint_repair_are_verified(self):
        merged = main._merge_sub2api_data([
            {"accounts": [{"name": "OAuth A", "platform": "openai", "type": "oauth", "credentials": {"access_token": "a"}}], "proxies": [{"proxy_key": "a"}]},
            {"type": "sub2api-bundle", "version": 1, "accounts": [{"name": "Setup B", "platform": "openai", "type": "setup-token", "credentials": {"setup_token": "b"}}], "proxies": []},
        ])
        self.assertEqual(len(merged["accounts"]), 2)
        self.assertEqual(len(merged["proxies"]), 1)

        normalized, mode, count = main._sub2api_apply_codex_fingerprint_mode(merged, "device")
        self.assertEqual((mode, count), ("device", 2))
        calls = []

        def fake_request(method, endpoint, payload=None, **_kwargs):
            calls.append((method, endpoint, payload))
            if endpoint.endswith("/bulk-update"):
                return 200, {"data": {"success_ids": [201]}}, ""
            account_type = "setup-token" if "type=setup-token" in endpoint else "oauth"
            fingerprint = "device" if len(calls) >= 4 else "off"
            items = [{
                "id": 201,
                "name": "OAuth A",
                "platform": "openai",
                "type": "oauth",
                "extra": {"codex_fingerprint_mode": fingerprint},
            }] if account_type == "oauth" else [{
                "id": 202,
                "name": "Setup B",
                "platform": "openai",
                "type": "setup-token",
                "extra": {"codex_fingerprint_mode": "device"},
            }]
            return 200, {"data": {"items": items, "pages": 1}}, ""

        with patch.object(main, "_external_json_request", side_effect=fake_request):
            verification = main._sub2api_reconcile_codex_fingerprint(
                {"base_url": "https://sub2api.example", "admin_key": "secret"},
                normalized,
                "device",
                {"data": {"results": [{"id": 201, "success": True}, {"id": 202, "success": True}]}},
            )

        self.assertEqual(verification["eligible"], 2)
        self.assertEqual(verification["verified"], 2)
        self.assertEqual(verification["repaired"], 1)
        self.assertEqual(verification["unresolved"], 0)
        bulk_payload = next(payload for method, endpoint, payload in calls if endpoint.endswith("/bulk-update"))
        self.assertEqual(bulk_payload, {"account_ids": [201], "extra": {"codex_fingerprint_mode": "device"}})

    def test_sub2api_401_reclaim_only_submits_explicit_401_errors(self):
        accounts = [
            {"id": 1, "name": "测试账号 team-EXAMPLE-401-CARD", "error_message": "OAuth 401: unauthorized"},
            {"id": 2, "name": "Second team-SECOND-401", "temp_unschedulable_reason": "Unauthorized (401)"},
            {"id": 3, "name": "Name user401", "error_message": "upstream timeout"},
            {"id": 4, "name": "Other team-NOT-USED", "error_message": "HTTP 500"},
            {"id": 5, "name": "No card suffix", "extra": {"last_response": {"status_code": 401}}},
        ]
        submitted = []

        class FakeClient:
            def batch_reclaim(self, card_codes, mode):
                submitted.append((card_codes, mode))
                return SimpleNamespace(ok=True, queued=len(card_codes))

        with patch.object(main, "sub2api_settings", return_value={"base_url": "https://sub2api.example", "admin_key": "secret"}), \
             patch.object(main, "_sub2api_fetch_accounts", return_value=accounts), \
             patch.object(main, "_redeem_client", return_value=FakeClient()):
            result = main.reclaim_sub2api_401_accounts()

        self.assertEqual(result["scanned_accounts"], 5)
        self.assertEqual(result["accounts_401"], 3)
        self.assertEqual(result["skipped_non_401"], 2)
        self.assertEqual(result["card_code_count"], 2)
        self.assertEqual(result["missing_card_code_count"], 1)
        self.assertEqual(submitted[0][1], "401")
        self.assertEqual(submitted[0][0][0], "team-EXAMPLE-401-CARD")
        self.assertNotIn("user401", submitted[0][0])

    def test_sub2api_401_reclaim_route_accepts_legacy_alias_and_surfaces_failure(self):
        handler = object.__new__(main.ApiHandler)
        handler.path = "/api/sub2api/reclaim401"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = BytesIO(b"{}")
        responses = []
        handler._send_json = lambda data, status=200: responses.append((status, data))
        with patch.object(main, "reclaim_sub2api_401_accounts", return_value={
            "ok": False,
            "scanned_accounts": 1,
            "accounts_401": 1,
            "result": {"ok": False, "error": "接口不存在"},
        }):
            handler.do_POST()
        status, payload = responses[-1]
        self.assertEqual(status, 502)
        self.assertEqual(payload["detail"], "接口不存在")

    def test_sub2api_account_list_is_mounted_on_the_http_handler(self):
        expected = {
            "ok": True,
            "items": [{"id": 8, "name": "account-8"}],
            "total": 1,
            "page": 2,
            "page_size": 12,
            "pages": 1,
            "usage": {},
            "usage_errors": {},
        }
        with patch.object(main, "fetch_sub2api_account_page", return_value=expected) as loader:
            status, payload = self.request_api(
                "GET",
                "/api/sub2api/accounts?page=2&page_size=12&search=account",
                {},
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        loader.assert_called_once_with({
            "page": ["2"],
            "page_size": ["12"],
            "search": ["account"],
        })

    def test_health_advertises_sub2api_capabilities(self):
        status, payload = self.request_api("GET", "/api/health", {})

        self.assertEqual(status, 200)
        self.assertTrue(payload["sub2api_accounts"])
        self.assertTrue(payload["sub2api_card_import_history"])
        self.assertTrue(payload["sub2api_card_import_history_delete"])
        self.assertTrue(payload["order_complaint_submit"])

    def test_sub2api_card_import_history_http_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            import_result = {
                "import_verification": {
                    "confirmed": True,
                    "expected": 2,
                    "matched": 2,
                    "failed": 0,
                    "new_account_ids": [11, 12],
                }
            }
            with patch.object(main, "DB_PATH", Path(directory) / "history.db"), patch.object(
                main, "_sub2api_import_payload", return_value=import_result
            ) as importer:
                main.init_database()
                status, created = self.request_api("POST", "/api/sub2api/card-import-records", {
                    "mode": "auto",
                    "card_codes": ["CARD-A", "CARD-B"],
                })
                self.assertEqual(status, 201)
                status, updated = self.request_api(
                    "PUT",
                    f"/api/sub2api/card-import-records/{created['id']}",
                    {
                        "status": "pending",
                        "stage": "ready",
                        "verified_count": 2,
                        "account_count": 2,
                        "message": "等待推送",
                        "retry_context": {
                            "data": {"accounts": [{"name": "a"}, {"name": "b"}]},
                            "proxy_id": 3,
                            "group_ids": [5],
                            "codex_fingerprint_mode": "device",
                            "assign_existing": True,
                            "reclaim_order_nos": ["ORDER-123"],
                        },
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(updated["retryable"])
                status, records = self.request_api(
                    "GET", "/api/sub2api/card-import-records?status=pending&page=1&page_size=10", {}
                )
                self.assertEqual(status, 200)
                self.assertEqual(records["items"][0]["card_codes"], ["CARD-A", "CARD-B"])
                self.assertNotIn("retry_context", records["items"][0])
                status, retried = self.request_api(
                    "POST", f"/api/sub2api/card-import-records/{created['id']}/retry", {}
                )
                self.assertEqual(status, 200)
                self.assertTrue(retried["ok"])
                self.assertEqual(retried["record"]["success_count"], 2)
                status, deleted = self.request_api(
                    "POST", "/api/sub2api/card-import-records/delete", {"id": created["id"]}
                )
                self.assertEqual(status, 200)
                self.assertEqual(deleted["deleted_ids"], [created["id"]])
                status, missing = self.request_api(
                    "POST", "/api/sub2api/card-import-records/delete", {"id": created["id"]}
                )
                self.assertEqual(status, 404)
                self.assertEqual(missing["detail"], "卡密导入记录不存在")
                batch_ids = []
                for code in ("CARD-C", "CARD-D"):
                    create_status, record = self.request_api(
                        "POST", "/api/sub2api/card-import-records", {
                            "mode": "manual", "card_codes": [code],
                        }
                    )
                    self.assertEqual(create_status, 201)
                    batch_ids.append(record["id"])
                status, batch_deleted = self.request_api(
                    "POST", "/api/sub2api/card-import-records/batch-delete", {"ids": batch_ids}
                )
                self.assertEqual(status, 200)
                self.assertEqual(batch_deleted["deleted_count"], 2)
                status, empty_records = self.request_api(
                    "GET", "/api/sub2api/card-import-records?page=1&page_size=10", {}
                )
                self.assertEqual(status, 200)
                self.assertEqual(empty_records["total"], 0)
        self.assertEqual(records["page"], 1)
        self.assertEqual(records["pages"], 1)
        importer.assert_called_once()
        self.assertEqual(importer.call_args.kwargs["proxy_id"], 3)

    def test_sub2api_401_text_recognizes_token_revoked_parenthesized_status(self):
        self.assertTrue(main._sub2api_account_is_401({
            "status": "error",
            "error_message": "Token revoked (401): Encountered invalidated oauth token",
        }))

    def test_sub2api_account_pagination_falls_back_to_total(self):
        calls = []

        def fake_request(method, endpoint, payload=None, **_kwargs):
            calls.append(endpoint)
            page = int(endpoint.split("page=", 1)[1].split("&", 1)[0])
            return 200, {"data": {"items": [{"id": page}], "total": 201, "page_size": 100}}, ""

        with patch.object(main, "_external_json_request", side_effect=fake_request):
            accounts = main._sub2api_fetch_accounts({"base_url": "https://sub2api.example", "admin_key": "secret"})

        self.assertEqual(len(calls), 3)
        self.assertEqual([item["id"] for item in accounts], [1, 2, 3])

    def test_sub2api_monitor_summary_reports_account_and_proxy_health(self):
        now = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
        accounts = [
            {"id": 1, "name": "Ready", "platform": "openai", "status": "active", "schedulable": True},
            {
                "id": 2, "name": "Limited", "platform": "openai", "status": "active", "schedulable": False,
                "rate_limit_reset_at": "2026-08-30T09:00:00Z", "expires_at": 1788253200,
            },
            {
                "id": 3, "name": "Broken", "platform": "anthropic", "status": "error", "schedulable": False,
                "error_message": "Token revoked (401)", "updated_at": "2026-08-30T07:30:00Z",
            },
        ]
        proxies = [
            {"status": "active", "latency_status": "success"},
            {"status": "active", "latency_status": "timeout", "expires_at": "2026-09-02T08:00:00Z"},
        ]
        groups = [{"status": "active"}, {"status": "inactive"}]

        result = main._sub2api_monitor_summary(accounts, proxies, groups, now=now)

        self.assertEqual(result["total_accounts"], 3)
        self.assertEqual(result["active_accounts"], 2)
        self.assertEqual(result["error_accounts"], 1)
        self.assertEqual(result["schedulable_accounts"], 1)
        self.assertEqual(result["rate_limited_accounts"], 1)
        self.assertEqual(result["unhealthy_proxies"], 1)
        self.assertEqual(result["expiring_proxies"], 1)
        self.assertEqual(result["inactive_groups"], 1)
        self.assertEqual(result["recent_errors"][0]["name"], "Broken")
        self.assertEqual(result["recent_errors"][0]["card_code"], "")
        self.assertTrue(result["recent_errors"][0]["is_401"])
        self.assertEqual(result["platforms"][0]["platform"], "openai")

    def test_sub2api_monitor_summary_exposes_401_card_code_for_manual_reclaim(self):
        result = main._sub2api_monitor_summary([
            {
                "id": 9,
                "name": "账号 team-6ca5c0-PTRW-087D100B983F",
                "platform": "openai",
                "status": "error",
                "error_message": "Authentication failed (401): token_invalidated",
            }
        ], [], [], now=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc))

        self.assertEqual(result["recent_errors"][0]["card_code"], "team-6ca5c0-PTRW-087D100B983F")
        self.assertTrue(result["recent_errors"][0]["is_401"])

    def test_sub2api_automation_requires_import_assignment_and_allows_passthrough(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                main._store_setting("sub2api", {"base_url": "https://sub2api.example", "admin_key": "secret"})
                base = {
                    "enabled": True,
                    "interval_seconds": 30,
                    "auto_import": True,
                    "proxy_id": 7,
                    "group_ids": [3],
                    "codex_fingerprint_mode": "session",
                }
                invalid = [
                    {**base, "proxy_id": None},
                    {**base, "group_ids": []},
                ]
                for payload in invalid:
                    with self.subTest(payload=payload), self.assertRaises(ValueError):
                        main.save_sub2api_automation_settings(payload)

                monitor_only = main.save_sub2api_automation_settings({
                    "enabled": True,
                    "interval_seconds": 30,
                    "auto_import": False,
                    "proxy_id": None,
                    "group_ids": [],
                    "codex_fingerprint_mode": "off",
                })
                self.assertTrue(monitor_only["enabled"])
                self.assertFalse(monitor_only["auto_import"])
                self.assertIsNone(monitor_only["proxy_id"])

                passthrough = main.save_sub2api_automation_settings({
                    **base,
                    "codex_fingerprint_mode": "off",
                })
                self.assertTrue(passthrough["enabled"])
                self.assertTrue(passthrough["auto_import"])
                self.assertEqual(passthrough["codex_fingerprint_mode"], "off")

                saved = main.save_sub2api_automation_settings({**base, "group_ids": [9, 3, 3]})
                self.assertTrue(saved["enabled"])
                self.assertEqual(saved["proxy_id"], 7)
                self.assertEqual(saved["group_ids"], [3, 9])
                self.assertEqual(main.sub2api_automation_settings(), saved)

    def test_sub2api_reclaim_download_and_progress_return_account_json(self):
        content = json.dumps({
            "type": "sub2api-data",
            "version": 1,
            "accounts": [{"name": "Recovered", "platform": "openai", "type": "oauth", "credentials": {"access_token": "token"}}],
            "proxies": [],
        }).encode("utf-8")

        class FakeClient:
            def refresh_progress(self, card_codes):
                self.card_codes = card_codes
                return SimpleNamespace(
                    ok=True,
                    queued=0,
                    already_running=0,
                    done=1,
                    all_tasks=[SimpleNamespace(
                        card_code=card_codes[0], order_no="ORDER-1", resource_uid="resource-1",
                        status="done", download_token="download-token", no_action=False,
                    )],
                )

            def download(self, order_no, token):
                self.download_args = (order_no, token)
                return content

        client = FakeClient()
        with patch.object(main, "_redeem_client", return_value=client):
            result = main.refresh_sub2api_reclaim(["team-CARD-1"])

        self.assertTrue(result["ok"])
        self.assertEqual(client.card_codes, ["team-CARD-1"])
        self.assertEqual(client.download_args, ("ORDER-1", "download-token"))
        self.assertEqual(result["downloaded_payloads"][0]["filename"], "ORDER-1.json")
        self.assertEqual(result["downloaded_payloads"][0]["data"]["accounts"][0]["name"], "Recovered")

    def test_sub2api_automation_cycle_refreshes_and_imports_recovered_accounts(self):
        recovered = {
            "type": "sub2api-data",
            "version": 1,
            "accounts": [{"name": "Recovered", "platform": "openai", "type": "oauth", "credentials": {"access_token": "token"}}],
            "proxies": [],
        }
        settings = {
            "enabled": True,
            "interval_seconds": 30,
            "auto_import": True,
            "proxy_id": 7,
            "group_ids": [3],
            "codex_fingerprint_mode": "full",
        }
        state = {
            "last_run": None,
            "last_error": "",
            "last_result": None,
            "pending_card_codes": ["team-CARD-1"],
            "imported_order_nos": [],
            "run_history": [],
        }
        reclaim = {
            "ok": True,
            "reclaim_card_codes": ["team-CARD-1"],
            "downloaded_payloads": [{"task": {"order_no": "ORDER-1"}, "data": recovered}],
            "result": {"queued": 0, "already_running": 0, "done": 1},
        }
        stored = []
        imported = {
            "ok": True,
            "mode": "assigned",
            "upstream_status": 200,
            "import_verification": {"confirmed": True, "matched": 1, "expected": 1},
        }
        with patch.object(main, "sub2api_automation_settings", return_value=settings), \
             patch.object(main, "sub2api_automation_state", return_value=state), \
             patch.object(main, "refresh_sub2api_reclaim", return_value=reclaim) as refresh, \
             patch.object(main, "_sub2api_import_payload", return_value=imported) as import_payload, \
             patch.object(main, "_store_sub2api_automation_state", side_effect=stored.append):
            result = main.run_sub2api_automation_cycle()

        refresh.assert_called_once_with(["team-CARD-1"], exclude_order_nos=[])
        import_payload.assert_called_once_with(
            [recovered], proxy_id=7, group_ids=[3], codex_fingerprint_mode="full", assign_existing=True
        )
        self.assertTrue(result["result"]["imported"])
        self.assertEqual(stored[-1]["pending_card_codes"], [])
        self.assertEqual(stored[-1]["imported_order_nos"], ["ORDER-1"])
        self.assertEqual(stored[-1]["run_history"][-1]["status"], "success")

    def test_sub2api_automation_and_reclaim_progress_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                main._store_setting("sub2api", {"base_url": "https://sub2api.example", "admin_key": "secret"})
                get_status, current = self.request_api("GET", "/api/sub2api/automation", {})
                self.assertEqual(get_status, 200)
                self.assertFalse(current["settings"]["enabled"])

                put_status, saved = self.request_api("PUT", "/api/sub2api/automation", {
                    "enabled": True,
                    "interval_seconds": 60,
                    "auto_import": True,
                    "proxy_id": 7,
                    "group_ids": [3],
                    "codex_fingerprint_mode": "device",
                })
                self.assertEqual(put_status, 200)
                self.assertTrue(saved["settings"]["enabled"])

                progress_payload = {"ok": True, "reclaim_card_codes": ["team-CARD-1"], "downloaded_payloads": [], "result": {"queued": 1}}
                with patch.object(main, "refresh_sub2api_reclaim", return_value=progress_payload) as refresh:
                    progress_status, progress = self.request_api(
                        "POST", "/api/sub2api/reclaim-progress", {"card_codes": ["team-CARD-1"]}
                    )
                self.assertEqual(progress_status, 200)
                self.assertEqual(progress, progress_payload)
                refresh.assert_called_once_with(["team-CARD-1"])

                run_payload = {"ok": True, "state": {"last_run": main.utc_now()}, "result": {"imported": False}}
                with patch.object(main.AUTOMATION_WORKER, "run_once", return_value=run_payload):
                    run_status, run_result = self.request_api("POST", "/api/sub2api/automation/run", {})
                self.assertEqual(run_status, 200)
                self.assertEqual(run_result, run_payload)

    def test_price_history_supports_filters_statistics_and_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = connection.execute(
                        "INSERT INTO watches(url, name, enabled) VALUES(?, ?, 1)",
                        ("https://pay.ldxp.cn/item/history-test", "历史测试"),
                    ).lastrowid
                    rows = [
                        ("2026-08-01T10:00:00", "5.00", "8", "on_sale", "success", None),
                        ("2026-08-02T10:00:00", "4.50", "0", "off_sale", "success", None),
                        ("2026-08-03T10:00:00", None, None, None, "error", "timeout"),
                    ]
                    connection.executemany(
                        """
                        INSERT INTO snapshots(
                            watch_id, title, price, stock, sale_status, fetched_at, status, error
                        ) VALUES(?, '历史测试', ?, ?, ?, ?, ?, ?)
                        """,
                        [(watch_id, price, stock, sale_status, fetched_at, status, error)
                         for fetched_at, price, stock, sale_status, status, error in rows],
                    )

                result = main.price_history(
                    watch_id,
                    limit=2,
                    offset=0,
                    start_date="2026-08-01",
                    end_date="2026-08-03",
                )
                self.assertEqual(result["total"], 3)
                self.assertEqual(len(result["items"]), 2)
                self.assertEqual(result["items"][0]["status"], "error")
                self.assertEqual(result["stats"]["success_count"], 2)
                self.assertEqual(result["stats"]["error_count"], 1)
                self.assertEqual(result["stats"]["in_stock_count"], 1)
                self.assertEqual(result["stats"]["out_stock_count"], 1)
                self.assertEqual(result["stats"]["unknown_stock_count"], 0)
                self.assertEqual(result["stats"]["min_price"], "4.50")
                self.assertEqual(result["stats"]["max_price"], "5.00")
                self.assertEqual(result["stats"]["first_price"], "5.00")
                self.assertEqual(result["stats"]["latest_price"], "4.50")
                self.assertEqual(result["stats"]["previous_price"], "5.00")
                self.assertEqual(result["stats"]["price_change"], "-0.50")
                self.assertEqual(result["stats"]["price_change_percent"], -10.0)
                self.assertEqual(result["stats"]["latest_change_percent"], -10.0)
                self.assertEqual(result["stats"]["volatility_percent"], 5.26)
                self.assertEqual(result["stats"]["price_change_count"], 1)
                self.assertEqual(result["stats"]["restock_count"], 0)
                self.assertEqual(result["stats"]["sold_out_count"], 1)
                self.assertEqual(result["stats"]["availability_rate"], 50.0)
                self.assertEqual(result["stats"]["first_at"], "2026-08-01T10:00:00")
                self.assertEqual(result["stats"]["latest_at"], "2026-08-02T10:00:00")

                status, payload = self.request_api(
                    "GET",
                    f"/api/watches/{watch_id}/history?limit=1&offset=0&stock=out&status=success",
                    {},
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["total"], 1)
                self.assertEqual(payload["items"][0]["stock"], 0)
                self.assertEqual(payload["trend"][0]["price"], "4.50")

    def test_price_history_tracks_price_and_inventory_events(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with patch.object(main, "database", side_effect=lambda: isolated_database(database_path)):
                main.init_database()
                with main.database() as connection:
                    watch_id = connection.execute(
                        "INSERT INTO watches(url, name, enabled) VALUES(?, ?, 1)",
                        ("https://pay.ldxp.cn/item/history-events", "价格事件测试"),
                    ).lastrowid
                    rows = [
                        ("2026-08-01T10:00:00", "10.00", "0"),
                        ("2026-08-02T10:00:00", "12.00", "4"),
                        ("2026-08-03T10:00:00", "12.00", None),
                        ("2026-08-04T10:00:00", "9.00", "0"),
                        ("2026-08-05T10:00:00", "9.00", "7"),
                    ]
                    connection.executemany(
                        """
                        INSERT INTO snapshots(watch_id, title, price, stock, sale_status, fetched_at, status)
                        VALUES(?, '价格事件测试', ?, ?, 'on_sale', ?, 'success')
                        """,
                        [(watch_id, price, stock, fetched_at) for fetched_at, price, stock in rows],
                    )

                result = main.price_history(watch_id, limit=25)
                self.assertEqual(result["stats"]["price_change_count"], 2)
                self.assertEqual(result["stats"]["restock_count"], 2)
                self.assertEqual(result["stats"]["sold_out_count"], 1)
                self.assertEqual(result["stats"]["availability_rate"], 50.0)
                self.assertEqual(result["stats"]["unknown_stock_count"], 1)
                self.assertEqual(result["stats"]["price_change"], "-1.00")
                self.assertEqual(result["stats"]["price_change_percent"], -10.0)
                self.assertEqual(result["stats"]["latest_change"], "0.00")
                self.assertEqual(len(result["trend"]), 5)

    def test_price_history_rejects_invalid_pagination(self):
        status, payload = self.request_api("GET", "/api/watches/1/history?limit=invalid", {})
        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"], "分页参数无效")


if __name__ == "__main__":
    unittest.main()
