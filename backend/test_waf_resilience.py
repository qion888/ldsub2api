from __future__ import annotations

import json
import tempfile
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch

from monitor_core import database as database_module
from monitor_core import storefront
from monitor_core.browser_verification import BrowserVerificationManager
from monitor_core import browser_verification as browser_verification_module
from monitor_core.inventory import InventoryService
from monitor_core.workers import MonitorWorker
from order_query.captcha import CaptchaRecognizer
from order_query.browser_verification import OrderQueryBrowserVerificationManager
from order_query.client import OrderQueryClient
from order_query.errors import OrderQueryWafVerificationRequired, UpstreamOrderError
from order_query.service import OrderQueryService
from order_query.sessions import OrderQuerySessionStore
from monitor_core.windows_input import DISPATCHED, NOT_READY


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

    def test_challenge_uses_one_bounded_native_slider_attempt(self) -> None:
        manager, _shop_id = self._browser_manager()

        class Driver:
            window_handles = ["window"]
            current_url = "https://pay.ldxp.cn/shop/STATUS"
            page_source = "<main>aliyunCaptcha</main>"

            def execute_script(self, _script):
                return True

            def get_cookies(self):
                return []

            def quit(self):
                return None

        manager.driver = Driver()
        manager.browser_process = object()
        with patch.object(browser_verification_module, "attempt_native_slider", return_value=DISPATCHED) as attempt:
            started = manager._render_challenge(manager.driver, "challenge")
            self.assertEqual(started["automatic_status"], DISPATCHED)
            self.assertTrue(started["automatic_attempted"])
            manager.poll("challenge-status")
            manager.poll("challenge-status")

        self.assertEqual(attempt.call_count, 1)

    def test_native_slider_not_ready_probes_three_times_then_stops(self) -> None:
        manager, _shop_id = self._browser_manager()

        class Driver:
            window_handles = ["window"]
            current_url = "https://pay.ldxp.cn/shop/STATUS"
            page_source = "<main>aliyunCaptcha</main>"

            def execute_script(self, _script):
                return True

            def get_cookies(self):
                return []

            def quit(self):
                return None

        manager.driver = Driver()
        manager.browser_process = object()
        with patch.object(browser_verification_module, "attempt_native_slider", return_value=NOT_READY) as attempt:
            started = manager._render_challenge(manager.driver, "challenge")
            self.assertEqual(started["automatic_status"], NOT_READY)
            for _ in range(5):
                manager.poll("challenge-status")

        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(manager._automatic_probe_count, 3)

    def test_native_slider_probe_budget_is_isolated_per_shop(self) -> None:
        manager, first_shop_id = self._browser_manager()
        manager.batch_current_shop_id = first_shop_id
        manager.browser_process = object()
        with patch.object(browser_verification_module, "attempt_native_slider", return_value=NOT_READY) as attempt:
            manager._attempt_automatic_slider(object())
            manager._attempt_automatic_slider(object())
            self.assertEqual(manager._automatic_probe_count, 2)

            manager.batch_current_shop_id = first_shop_id + 1
            manager._attempt_automatic_slider(object())

        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(manager._automatic_probe_count, 1)

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


class OrderQueryBrowserVerificationTests(unittest.TestCase):
    def _session_and_manager(self):
        store = OrderQuerySessionStore(clock=lambda: 100.0)
        client = type("Client", (), {"cookie_jar": CookieJar(), "visitor_id": "visitor_1"})()
        session = store.create("buyer@example.com", client)
        session.ticket = "ticket"
        manager = OrderQueryBrowserVerificationManager(
            sessions=store,
            profile_path=Path(tempfile.gettempdir()) / "order-query-waf-tests",
        )
        manager.session_id = session.session_id
        manager.keywords = "buyer@example.com"
        manager.request = {"status": 999, "page": 1, "page_size": 10}
        return store, session, manager

    def test_browser_cookie_sync_accepts_both_official_hosts_and_parent_domain(self) -> None:
        self.assertTrue(OrderQueryBrowserVerificationManager._is_storefront_cookie_domain("pay.ldxp.cn"))
        self.assertTrue(OrderQueryBrowserVerificationManager._is_storefront_cookie_domain("wzyp.cn"))
        self.assertTrue(OrderQueryBrowserVerificationManager._is_storefront_cookie_domain(".ldxp.cn"))
        self.assertFalse(OrderQueryBrowserVerificationManager._is_storefront_cookie_domain("example.com"))

    def test_challenge_replay_keeps_the_json_api_contract(self) -> None:
        _store, session, manager = self._session_and_manager()

        class Driver:
            window_handles = ["window"]
            current_url = "https://pay.ldxp.cn/order"
            page_source = '<div id="aliyunCaptcha"></div>'

            def __init__(self) -> None:
                self.scripts = []

            def execute_script(self, script, *_args):
                self.scripts.append(script)
                return True if "querySelectorAll" in script else None

            def get_cookies(self):
                return [{"name": "acw_tc", "value": "pending", "domain": ".ldxp.cn", "path": "/"}]

        driver = Driver()
        result = manager._render_challenge(
            driver,
            "<html>captured response must not be injected</html>",
            session,
            {"keywords": "buyer@example.com", "status": 999, "page": 1, "page_size": 10},
        )

        self.assertEqual(result["status"], "awaiting_verification")
        submitted = driver.scripts[0]
        self.assertIn("'Content-Type': 'application/json'", submitted)
        self.assertIn("JSON.stringify(payload)", submitted)
        self.assertIn("document.open('text/html', 'replace')", submitted)
        self.assertIn("document.write(text)", submitted)
        self.assertNotIn("form.enctype", submitted)

    def test_status_waits_for_stable_verified_waf_cookie_before_replay(self) -> None:
        _store, session, manager = self._session_and_manager()

        class Driver:
            window_handles = ["window"]
            current_url = "https://pay.ldxp.cn/shopApi/Order/list"
            page_source = "<main>verification complete</main>"

            def execute_script(self, _script):
                return False

            def get_cookies(self):
                return [{"name": "acw_tc", "value": "verified", "domain": ".ldxp.cn", "path": "/"}]

        manager.driver = Driver()
        manager._challenge_dom_seen = True
        manager._challenge_cookie_baseline = manager._cookie_fingerprint(
            [{"name": "acw_tc", "value": "pending", "domain": ".ldxp.cn", "path": "/"}]
        )
        request = {"session_id": session.session_id, "keywords": "buyer@example.com", "status": 999, "page": 1, "page_size": 10}

        first = manager.status(request)
        second = manager.status(request)

        self.assertEqual(first["status"], "awaiting_verification")
        self.assertEqual(second["status"], "ready")
        self.assertTrue(second["ready"])

    def test_waf_context_renews_the_lookup_session_before_opening_browser(self) -> None:
        service = OrderQueryService()
        session = service.sessions.create("buyer@example.com", type("Client", (), {})())
        request = {"keywords": "buyer@example.com", "status": 999, "page": 1, "page_size": 10}

        with self.assertRaises(OrderQueryWafVerificationRequired) as context:
            service._with_waf_context(
                session,
                request,
                lambda: (_ for _ in ()).throw(storefront.WafChallengeRequired("WAF")),
            )

        self.assertGreaterEqual(context.exception.expires_in, 899)

    def test_browser_start_accepts_a_session_before_ticket_creation(self) -> None:
        store, session, manager = self._session_and_manager()
        session.ticket = None

        class Driver:
            window_handles = ["window"]
            current_url = "https://pay.ldxp.cn/order"
            page_source = "<main>order page</main>"

            def __init__(self) -> None:
                self.calls = 0

            def get(self, _url):
                return None

            def get_cookies(self):
                return []

            def set_script_timeout(self, _seconds):
                return None

            def execute_async_script(self, _script, *_args):
                self.calls += 1
                if self.calls == 1:
                    return {"status": 200, "content_type": "application/json", "text": '{"code":1,"data":{}}'}
                return {"status": 200, "content_type": "application/json", "text": '{"code":0,"msg":"ticket required"}'}

            def quit(self):
                return None

        driver = Driver()
        manager._create_driver = lambda: (driver, "edge")
        result = manager.start(
            {
                "session_id": session.session_id,
                "keywords": "buyer@example.com",
                "status": 999,
                "page": 1,
                "page_size": 10,
            }
        )

        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["resume_search"])
        self.assertEqual(driver.calls, 2)


class OrderQueryCaptchaUrlTests(unittest.TestCase):
    def test_captcha_urls_accept_all_supported_storefront_hosts(self) -> None:
        image_url = OrderQueryClient._validated_url(
            "https://wzyp.cn/shopApi/common/captchaImg.html?key=fixture",
            {"/shopApi/common/captchaImg.html"},
        )
        check_url = OrderQueryClient._validated_url(
            "https://pay.ldxp.cn/shopApi/common/captchaCheck.html?key=fixture",
            {"/shopApi/common/captchaCheck.html"},
        )

        self.assertEqual(image_url, "https://wzyp.cn/shopApi/common/captchaImg.html?key=fixture")
        self.assertEqual(check_url, "https://pay.ldxp.cn/shopApi/common/captchaCheck.html?key=fixture")

    def test_captcha_urls_normalize_relative_upstream_values(self) -> None:
        self.assertEqual(
            OrderQueryClient._validated_url(
                "/shopApi/common/captchaImg.html?key=fixture",
                {"/shopApi/common/captchaImg.html"},
            ),
            "https://pay.ldxp.cn/shopApi/common/captchaImg.html?key=fixture",
        )
        self.assertEqual(
            OrderQueryClient._validated_url(
                "//wzyp.cn/shopApi/common/captchaCheck.html?key=fixture",
                {"/shopApi/common/captchaCheck.html"},
            ),
            "https://wzyp.cn/shopApi/common/captchaCheck.html?key=fixture",
        )

    def test_captcha_url_rejects_untrusted_hosts(self) -> None:
        with self.assertRaises(UpstreamOrderError) as context:
            OrderQueryClient._validated_url(
                "https://example.com/shopApi/common/captchaImg.html?key=fixture",
                {"/shopApi/common/captchaImg.html"},
            )

        self.assertEqual(context.exception.code, "invalid_captcha_url")


class OrderQueryOcrRecoveryTests(unittest.TestCase):
    def test_recognizer_retries_an_unreadable_image_with_filtered_foreground(self) -> None:
        calls: list[bytes] = []

        class Classifier:
            def classification(self, image: bytes) -> str:
                calls.append(image)
                return "bad" if image == b"source" else "Z9x8"

        recognizer = CaptchaRecognizer(lambda: Classifier())
        with patch.object(CaptchaRecognizer, "_saturated_foreground_png", return_value=b"filtered"):
            self.assertEqual(recognizer.recognize_candidates(b"source"), ["Z9x8"])

        self.assertEqual(calls, [b"source", b"filtered"])

    def test_automatic_ticket_uses_five_bounded_challenges_before_manual_fallback(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.starts = 0
                self.checked: list[str] = []

            def start_captcha(self, previous_code: str = ""):
                self.starts += 1
                return type("Challenge", (), {"image_url": "", "check_url": "", "ip": ""})()

            def download_captcha(self, _challenge):
                return b"captcha", "image/png"

            def check_captcha(self, _challenge, code: str):
                self.checked.append(code)
                return "ticket" if code == "AB12" else None

        class Recognizer:
            def __init__(self) -> None:
                self.codes = iter(["AA00", "BB11", "CC22", "DD33", "AB12"])

            def recognize(self, _image: bytes) -> str:
                return next(self.codes)

        client = Client()
        service = OrderQueryService(client_factory=lambda: client, recognizer=Recognizer(), max_ocr_attempts=5)
        session = service.sessions.create("buyer@example.com", client)

        ticket, attempts = service._automatic_ticket(session, attempts_used=0)

        self.assertEqual(ticket, "ticket")
        self.assertEqual(attempts, 5)
        self.assertEqual(client.starts, 5)
        self.assertEqual(client.checked, ["AA00", "BB11", "CC22", "DD33", "AB12"])


if __name__ == "__main__":
    unittest.main()
