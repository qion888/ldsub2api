from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from monitor_core.storefront import WafChallengeRequired
from order_query.browser_verification import OrderQueryBrowserVerificationManager
from order_query.routes import ORDER_WAF_STATUS_PATH, request_rejection
from order_query.sessions import OrderQuerySessionStore


class FakeClient:
    cookie_jar: list[Any] = []
    visitor_id = "visitor123"


class FakeDriver:
    def __init__(self) -> None:
        self.page_source = "<html>滑块验证</html>"
        self.get_calls: list[str] = []
        self.quit_calls = 0

    def get(self, url: str) -> None:
        self.get_calls.append(url)

    def add_cookie(self, value: dict[str, Any]) -> None:
        return None

    def get_cookies(self) -> list[dict[str, Any]]:
        return []

    def execute_script(self, script: str, value: str) -> None:
        return None

    def quit(self) -> None:
        self.quit_calls += 1


class OrderQueryWafTests(unittest.TestCase):
    def test_renew_extends_interactive_verification_lease(self) -> None:
        clock = [100.0]
        sessions = OrderQuerySessionStore(ttl_seconds=60, cache_seconds=0, clock=lambda: clock[0])
        session = sessions.create("buyer", FakeClient())
        clock[0] = 150.0

        remaining = sessions.renew(session, ttl_seconds=900)

        self.assertEqual(remaining, 900)
        self.assertEqual(sessions.remaining(session), 900)

    def test_browser_uses_ephemeral_profile_and_reports_ready_without_api_polling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "profiles"
            sessions = OrderQuerySessionStore(ttl_seconds=60, cache_seconds=0)
            session = sessions.create("buyer", FakeClient())
            session.ticket = "ticket-ok"
            driver = FakeDriver()
            manager = OrderQueryBrowserVerificationManager(sessions=sessions, profile_path=base)
            profile_paths: list[Path] = []

            def create_driver() -> tuple[FakeDriver, str]:
                base.mkdir(parents=True, exist_ok=True)
                profile = Path(tempfile.mkdtemp(prefix="order-waf-", dir=base))
                manager._active_profile_path = profile
                profile_paths.append(profile)
                return driver, "edge"

            manager._create_driver = create_driver  # type: ignore[method-assign]

            def challenge_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
                raise WafChallengeRequired("challenge")

            manager._browser_request = challenge_request  # type: ignore[method-assign]
            request = {
                "session_id": session.session_id,
                "keywords": "buyer",
                "status": 999,
                "page": 1,
                "page_size": 10,
            }
            started = manager.start(request)
            self.assertEqual(started["status"], "awaiting_verification")
            self.assertTrue(profile_paths[0].exists())
            self.assertEqual(manager.status(request)["status"], "awaiting_verification")

            driver.page_source = "<html><body>订单查询</body></html>"
            self.assertTrue(manager.status(request)["ready"])

            manager._browser_request = lambda *args, **kwargs: {"code": 1, "data": {"list": [], "total": 0}}  # type: ignore[method-assign]
            completed = manager.complete(request)
            self.assertEqual(completed["status"], "success")
            self.assertFalse(profile_paths[0].exists())
            self.assertGreaterEqual(driver.quit_calls, 1)

    def test_status_endpoint_accepts_json_local_requests(self) -> None:
        self.assertIsNone(
            request_rejection(
                ORDER_WAF_STATUS_PATH,
                {"Content-Type": "application/json", "Origin": "http://127.0.0.1:5173"},
                frontend_url="http://127.0.0.1:5173/",
            )
        )


if __name__ == "__main__":
    unittest.main()
