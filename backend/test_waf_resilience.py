from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor_core import database as database_module
from monitor_core import storefront
from monitor_core.browser_verification import BrowserVerificationManager
from monitor_core.inventory import InventoryService
from monitor_core.workers import MonitorWorker


class WafResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "monitor.db"
        self.database = lambda: database_module.create_database(self.db_path)
        database_module.initialize_database(
            self.database,
            now=lambda: "2026-09-02T08:00:00+00:00",
            default_interval=300,
            default_redeem_url="https://redeem.example",
            default_sub2api_url="https://sub2api.example",
            default_automation={"enabled": False},
        )
        with storefront._BROWSER_SESSION_LOCK:
            storefront._BROWSER_SESSION.clear()

    def tearDown(self) -> None:
        with storefront._BROWSER_SESSION_LOCK:
            storefront._BROWSER_SESSION.clear()
        self.directory.cleanup()

    def test_verified_browser_state_is_reused_without_foreign_or_expired_cookies(self) -> None:
        with patch.object(storefront.time, "time", return_value=1000.0):
            accepted = storefront.remember_browser_session(
                [
                    {"name": "acw_tc", "value": "verified", "domain": ".ldxp.cn", "expiry": 2000},
                    {"name": "expired", "value": "old", "domain": "pay.ldxp.cn", "expiry": 900},
                    {"name": "foreign", "value": "no", "domain": "example.com", "expiry": 2000},
                ],
                "Mozilla/5.0 Browser",
                "visitor_test_1",
            )

        self.assertEqual(accepted, 1)
        self.assertEqual(
            storefront.browser_session_headers(now=1100),
            {
                "User-Agent": "Mozilla/5.0 Browser",
                "Cookie": "acw_tc=verified",
                "Visitorid": "visitor_test_1",
            },
        )
        self.assertEqual(storefront.browser_session_headers(now=1000 + 13 * 60 * 60), {})

    def test_storefront_request_uses_verified_browser_headers(self) -> None:
        with patch.object(storefront.time, "time", return_value=1000.0):
            storefront.remember_browser_session(
                [{"name": "waf_session", "value": "passed", "domain": "pay.ldxp.cn"}],
                "Verified Browser UA",
                "visitor_saved",
            )

        captured = {}

        class Response:
            headers = {"Content-Type": "application/json"}
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps({"code": 1, "data": []}).encode("utf-8")

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        with patch.object(storefront.time, "time", return_value=1001.0):
            result = storefront._post_shop_api(
                "/shopApi/Shop/goodsList",
                {"token": "TEST"},
                "https://pay.ldxp.cn/shop/TEST",
                opener=opener,
            )

        request = captured["request"]
        self.assertEqual(result["code"], 1)
        self.assertEqual(request.get_header("User-agent"), "Verified Browser UA")
        self.assertEqual(request.get_header("Cookie"), "waf_session=passed")
        self.assertEqual(request.get_header("Visitorid"), "visitor_saved")

    def test_failed_shop_refresh_advances_all_linked_watch_timestamps(self) -> None:
        stamp = "2026-09-02T08:05:00+00:00"
        with self.database() as connection:
            shop_id = connection.execute(
                "INSERT INTO shops(url, token, interval_seconds, created_at) VALUES(?, ?, 300, ?)",
                ("https://pay.ldxp.cn/shop/WAFTEST", "WAFTEST", stamp),
            ).lastrowid
            watch_ids = []
            for suffix in ("one", "two"):
                watch_id = connection.execute(
                    "INSERT INTO watches(url, enabled, interval_seconds, created_at) VALUES(?, 1, 300, ?)",
                    (f"https://pay.ldxp.cn/item/{suffix}", stamp),
                ).lastrowid
                watch_ids.append(watch_id)
                connection.execute(
                    "INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen) VALUES(?, ?, ?, 1, ?)",
                    (shop_id, suffix, watch_id, stamp),
                )

        service = InventoryService(
            database=self.database,
            now=lambda: stamp,
            fetch_goods=lambda _url: {},
            fetch_shop_catalog=lambda _url, **_kwargs: (_ for _ in ()).throw(
                storefront.WafChallengeRequired("WAF challenge required")
            ),
            commerce_tags=lambda _item: [],
            is_unlisted_error=lambda _value: False,
            sync_intervals=lambda _connection, _shop_id, _interval: 0,
        )

        with self.assertRaisesRegex(RuntimeError, "WAF challenge required"):
            service.record_shop_fetch(shop_id)

        with self.database() as connection:
            last_runs = [
                connection.execute("SELECT last_run FROM watches WHERE id = ?", (watch_id,)).fetchone()[0]
                for watch_id in watch_ids
            ]
        self.assertEqual(last_runs, [stamp, stamp])

    def test_worker_uses_hour_backoff_for_linked_waf_failures(self) -> None:
        worker = MonitorWorker(
            database=self.database,
            record_inventory_fetch=lambda *_args: {},
            record_shop_fetch=lambda *_args: {},
            process_preorder=lambda *_args: None,
            mark_preorder_check_error=lambda *_args: None,
            default_interval=300,
        )

        self.assertEqual(worker._retry_interval(300, 300, "Aliyun WAF 滑块验证"), 3600)
        self.assertEqual(worker._retry_interval(300, 300, "connection reset"), 300)

    def _browser_manager(self) -> tuple[BrowserVerificationManager, int]:
        with self.database() as connection:
            shop_id = connection.execute(
                "INSERT INTO shops(url, token, interval_seconds, created_at) VALUES(?, ?, 300, ?)",
                ("https://pay.ldxp.cn/shop/STATUS", "STATUS", "2026-09-02T08:00:00+00:00"),
            ).lastrowid
        manager = BrowserVerificationManager(
            database=self.database,
            worker_lock=object(),
            record_shop_fetch=lambda *_args, **_kwargs: {},
            goods_list_rows=lambda _payload: ([], {}),
            normalize_goods=lambda item, _token: item,
            first_value=lambda _item, _keys: None,
            waf_error=storefront.WafChallengeRequired,
            waf_markers=storefront.WAF_MARKERS,
            profile_path=Path(self.directory.name) / "waf-profile",
        )
        manager.batch_active = True
        manager.batch_total = 1
        manager.batch_shop_ids = (shop_id,)
        manager.batch_queue = [shop_id]
        manager.batch_current_shop_id = shop_id
        manager.challenge_id = "challenge-status"
        return manager, shop_id

    def test_browser_status_is_passive_and_requires_real_verification_evidence(self) -> None:
        manager, _shop_id = self._browser_manager()

        class Driver:
            window_handles = ["window"]
            current_url = "https://pay.ldxp.cn/shop/STATUS"
            page_source = "<main>ordinary storefront</main>"

            def get_cookies(self):
                return []

            def quit(self):
                return None

        manager.driver = Driver()
        manager._browser_request = lambda *_args: (_ for _ in ()).throw(
            AssertionError("status polling must not request goodsList")
        )

        first = manager.poll("challenge-status")
        second = manager.poll("challenge-status")

        self.assertEqual(first["status"], "awaiting_verification")
        self.assertEqual(second["status"], "awaiting_verification")
        self.assertEqual(second["observation_count"], 0)

    def test_browser_status_becomes_ready_after_verified_cookie_change_is_stable(self) -> None:
        manager, _shop_id = self._browser_manager()

        class Driver:
            window_handles = ["window"]
            current_url = "https://pay.ldxp.cn/shop/STATUS"
            page_source = "<main>storefront</main>"

            def get_cookies(self):
                return [{"name": "acw_tc", "value": "verified", "domain": ".ldxp.cn", "path": "/"}]

            def quit(self):
                return None

        manager.driver = Driver()
        manager._challenge_cookie_baseline = manager._cookie_fingerprint(
            [{"name": "acw_tc", "value": "pending", "domain": ".ldxp.cn", "path": "/"}]
        )

        first = manager.poll("challenge-status")
        second = manager.poll("challenge-status")

        self.assertEqual(first["status"], "awaiting_verification")
        self.assertEqual(second["status"], "ready")
        self.assertTrue(second["ready"])

    def test_browser_status_reports_closed_window_without_restarting_session(self) -> None:
        manager, _shop_id = self._browser_manager()

        class Driver:
            window_handles = []

            def quit(self):
                return None

        manager.driver = Driver()
        result = manager.poll("challenge-status")

        self.assertEqual(result["status"], "browser_closed")
        self.assertFalse(manager.batch_active)
        self.assertIn("closed", result["detail"].lower())

    def test_completed_challenge_is_idempotent_and_stale_ids_are_rejected(self) -> None:
        manager, _shop_id = self._browser_manager()
        manager.batch_active = False
        manager.driver = None
        manager._last_completed_challenge_id = "challenge-status"
        manager._last_completed_result = {"status": "success", "completed": 1, "total": 1}

        self.assertEqual(manager.complete_all("challenge-status")["status"], "success")
        with self.assertRaisesRegex(RuntimeError, "changed|pending"):
            manager.complete_all("challenge-stale")

    def test_completion_replay_limit_is_hard_even_after_ready_observation(self) -> None:
        manager, _shop_id = self._browser_manager()

        class Driver:
            window_handles = ["window"]

            def quit(self):
                return None

        manager.driver = Driver()
        manager._completion_replays = manager.MAX_COMPLETION_REPLAYS
        manager._challenge_ready = True
        manager._advance_batch_locked = lambda: (_ for _ in ()).throw(
            AssertionError("retry exhaustion must not replay goodsList")
        )

        result = manager.complete_all("challenge-status")

        self.assertEqual(result["status"], "retry_exhausted")
        self.assertFalse(manager.batch_active)
        self.assertIn("exhausted", result["detail"].lower())

    def test_render_challenge_probes_accessible_storefront_before_waiting(self) -> None:
        manager, shop_id = self._browser_manager()

        class Driver:
            window_handles = ["window"]
            current_url = ""
            page_source = "<main>ordinary storefront</main>"

            def get(self, url):
                self.current_url = url

            def get_cookies(self):
                return []

            def quit(self):
                return None

        driver = Driver()
        probe_payload = {"code": 1, "data": {"list": []}}
        observed = {}
        manager.driver = driver
        manager._browser_request = lambda _driver, _data: probe_payload
        manager._sync_shop = lambda _driver, current_shop_id, *, first_payload=None: observed.update(
            shop_id=current_shop_id, first_payload=first_payload
        ) or {"status": "success", "product_count": 0}
        manager._advance_batch_locked = lambda: {"status": "success", "completed": 1, "total": 1}

        result = manager._render_challenge(driver, "waf response")

        self.assertEqual(result["status"], "success")
        self.assertEqual(observed, {"shop_id": shop_id, "first_payload": probe_payload})
        self.assertEqual(manager.batch_results[0]["id"], shop_id)
        self.assertTrue(manager.batch_results[0]["ok"])

    def test_render_challenge_waits_when_browser_probe_is_still_waf(self) -> None:
        manager, _shop_id = self._browser_manager()

        class Driver:
            window_handles = ["window"]
            current_url = ""
            page_source = "<main>ordinary storefront</main>"

            def get(self, url):
                self.current_url = url

            def get_cookies(self):
                return []

            def quit(self):
                return None

        manager.driver = Driver()
        manager._browser_request = lambda *_args: (_ for _ in ()).throw(
            storefront.WafChallengeRequired("WAF challenge required")
        )

        result = manager._render_challenge(manager.driver, "waf response")

        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(manager.challenge_attempts, 1)
        self.assertTrue(manager._challenge_dom_seen)


if __name__ == "__main__":
    unittest.main()
