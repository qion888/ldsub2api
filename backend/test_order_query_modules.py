from __future__ import annotations

import json
import unittest
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.request import Request

import main
from order_query import routes
from order_query.captcha import CaptchaRecognizer, captcha_sign, normalize_captcha_code
from order_query.client import CaptchaChallenge, OrderQueryClient
from order_query.errors import CaptchaRecognizerUnavailable, OrderQueryInputError
from order_query.service import OrderQueryService
from order_query.sessions import OrderQuerySessionStore


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self.body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float) -> FakeResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class FakeRecognizer:
    def __init__(self, values: list[Any]) -> None:
        self.values = list(values)
        self.calls = 0

    def recognize(self, image: bytes) -> str:
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return str(value)


class FakeOrderClient:
    def __init__(self) -> None:
        self.start_count = 0
        self.check_codes: list[str] = []
        self.list_calls: list[dict[str, Any]] = []
        self.expire_next_list = False

    def start_captcha(self, previous_code: str = "") -> CaptchaChallenge:
        self.start_count += 1
        return CaptchaChallenge(
            image_url=f"https://pay.ldxp.cn/image/{self.start_count}",
            check_url=f"https://pay.ldxp.cn/check/{self.start_count}",
            ip="127.0.0.1",
        )

    def download_captcha(self, challenge: CaptchaChallenge) -> tuple[bytes, str]:
        return f"image-{self.start_count}".encode("ascii"), "image/png"

    def check_captcha(self, challenge: CaptchaChallenge, code: str) -> str | None:
        self.check_codes.append(code)
        return "ticket-ok" if code == "AB12" else None

    def list_orders(self, **kwargs: Any) -> dict[str, Any]:
        from order_query.errors import CaptchaVerificationExpired

        self.list_calls.append(kwargs)
        if self.expire_next_list:
            self.expire_next_list = False
            raise CaptchaVerificationExpired()
        return {
            "orders": [{"trade_no": "ORDER-1", "status": kwargs["status"]}],
            "pagination": {
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
                "total": 1,
                "pages": 1,
            },
        }


class CaptchaTests(unittest.TestCase):
    def test_sign_matches_storefront_javascript(self) -> None:
        self.assertEqual(
            captcha_sign("A1b2", "223.104.78.191"),
            "8759a73371a8d08f3ef0b1292843d0a9",
        )

    def test_code_is_strictly_four_ascii_alphanumeric_characters(self) -> None:
        self.assertEqual(normalize_captcha_code(" A1b2\n"), "A1b2")
        for value in ("123", "12345", "１２３４", "ab-1", "中文验证码"):
            self.assertEqual(normalize_captcha_code(value), "")

    def test_recognizer_loads_injected_classifier_lazily(self) -> None:
        loads: list[bool] = []

        class Classifier:
            def classification(self, image: bytes) -> str:
                return " Z9x8 "

        recognizer = CaptchaRecognizer(lambda: loads.append(True) or Classifier())
        self.assertEqual(loads, [])
        self.assertEqual(recognizer.recognize(b"png"), "Z9x8")
        self.assertEqual(recognizer.recognize(b"png"), "Z9x8")
        self.assertEqual(loads, [True])


class ClientTests(unittest.TestCase):
    @staticmethod
    def _json_response(value: Any) -> FakeResponse:
        return FakeResponse(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def test_one_client_handles_complete_cookie_session_and_normalizes_orders(self) -> None:
        opener = FakeOpener([
            self._json_response({
                "code": 1,
                "data": {
                    "img_url": "https://pay.ldxp.cn/shopApi/common/captchaImg.html?key=image",
                    "check_url": "https://pay.ldxp.cn/shopApi/common/captchaCheck.html?key=check",
                    "ip": "223.104.78.191",
                },
            }),
            FakeResponse(b"png-bytes", "image/png; charset=utf-8"),
            self._json_response({"code": 1, "data": {"ticket": "ticket-value"}}),
            self._json_response({
                "code": 1,
                "data": {
                    "total": 1,
                    "list": [{
                        "trade_no": "LD-100",
                        "goods_name": "测试商品",
                        "create_time": "2026-08-30T19:20:21+08:00",
                        "total_amount": "9.9",
                        "quantity": "2",
                        "status": 1,
                        "need_query_password": 1,
                        "can_complaint": 0,
                        "private_contact": "must-not-leak",
                        "goods": {
                            "goods_key": "GOODS1",
                            "goods_type": "card",
                            "image": "https://cdn.example.test/goods.png",
                            "secret_cards": ["must-not-leak"],
                        },
                        "complaint": {"status": -1, "messages": ["must-not-leak"]},
                    }],
                },
            }),
        ])
        client = OrderQueryClient(opener=opener)

        challenge = client.start_captcha()
        image, mime_type = client.download_captcha(challenge)
        ticket = client.check_captcha(challenge, "A1b2")
        result = client.list_orders(
            keywords="buyer@example.test",
            ticket=str(ticket),
            status=999,
            page=1,
            page_size=10,
        )

        self.assertEqual(image, b"png-bytes")
        self.assertEqual(mime_type, "image/png")
        self.assertEqual(ticket, "ticket-value")
        self.assertEqual(len(opener.requests), 4)
        visitor_ids = []
        for request in opener.requests:
            headers = {key.lower(): value for key, value in request.header_items()}
            visitor_ids.append(headers["visitorid"])
        self.assertEqual(visitor_ids, [client.visitor_id] * 4)
        self.assertRegex(client.visitor_id, r"^[a-z0-9]{9}$")
        order = result["orders"][0]
        self.assertEqual(order["created_at"], "2026-08-30T11:20:21+00:00")
        self.assertEqual(order["total_amount"], "9.90")
        self.assertEqual(order["status_label"], "已付款")
        self.assertNotIn("private_contact", order)
        self.assertNotIn("secret_cards", json.dumps(order, ensure_ascii=False))
        self.assertNotIn("messages", json.dumps(order, ensure_ascii=False))

        check_payload = json.loads(opener.requests[2].data.decode("utf-8"))
        self.assertEqual(check_payload["sign"], captcha_sign("A1b2", challenge.ip))
        list_payload = json.loads(opener.requests[3].data.decode("utf-8"))
        self.assertEqual(list_payload["current"], 1)
        self.assertEqual(list_payload["pageSize"], 10)

    def test_dynamic_captcha_urls_reject_other_hosts(self) -> None:
        opener = FakeOpener([
            self._json_response({
                "code": 1,
                "data": {
                    "img_url": "https://example.test/shopApi/common/captchaImg.html?key=x",
                    "check_url": "https://pay.ldxp.cn/shopApi/common/captchaCheck.html?key=x",
                    "ip": "127.0.0.1",
                },
            }),
        ])
        with self.assertRaisesRegex(RuntimeError, "地址无效"):
            OrderQueryClient(opener=opener).start_captcha()

    def test_empty_order_result_keeps_one_pagination_page(self) -> None:
        from order_query.client import normalize_order_list

        result = normalize_order_list(
            {"code": 1, "data": {"total": 0, "list": []}},
            page=1,
            page_size=10,
        )
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["pagination"], {
            "page": 1,
            "page_size": 10,
            "total": 0,
            "pages": 1,
        })

    def test_captcha_image_rejects_non_raster_mime_type(self) -> None:
        client = OrderQueryClient(opener=FakeOpener([
            FakeResponse(b"<svg/>", "image/svg+xml"),
        ]))
        challenge = CaptchaChallenge(
            image_url="https://pay.ldxp.cn/shopApi/common/captchaImg.html?key=image",
            check_url="https://pay.ldxp.cn/shopApi/common/captchaCheck.html?key=check",
            ip="127.0.0.1",
        )
        with self.assertRaisesRegex(RuntimeError, "图片响应无效"):
            client.download_captcha(challenge)


class ServiceTests(unittest.TestCase):
    @staticmethod
    def service(client: FakeOrderClient, recognizer: FakeRecognizer) -> OrderQueryService:
        return OrderQueryService(
            client_factory=lambda: client,
            recognizer=recognizer,
            sessions=OrderQuerySessionStore(ttl_seconds=120, cache_seconds=5),
            max_ocr_attempts=3,
        )

    def test_auto_ocr_retries_then_ticket_is_reused_for_filters_and_pages(self) -> None:
        client = FakeOrderClient()
        recognizer = FakeRecognizer(["ZZ99", "AB12"])
        service = self.service(client, recognizer)

        first = service.search({"keywords": "buyer", "status": 999, "page": 1, "page_size": 10})
        second = service.search({
            "keywords": "buyer",
            "status": 1,
            "page": 2,
            "page_size": 10,
            "session_id": first["session_id"],
        })
        cached = service.search({
            "keywords": "buyer",
            "status": 1,
            "page": 2,
            "page_size": 10,
            "session_id": first["session_id"],
        })

        self.assertEqual(first["verification"], {"status": "verified", "mode": "ocr", "attempts": 2})
        self.assertEqual(second["verification"], {"status": "verified", "mode": "session", "attempts": 0})
        self.assertEqual(cached["orders"], second["orders"])
        self.assertEqual(client.start_count, 2)
        self.assertEqual(client.check_codes, ["ZZ99", "AB12"])
        self.assertEqual(len(client.list_calls), 2)
        self.assertEqual(client.list_calls[1]["ticket"], "ticket-ok")

    def test_three_failed_ocr_attempts_return_manual_challenge_then_accept_code(self) -> None:
        client = FakeOrderClient()
        recognizer = FakeRecognizer(["AAAA", "BBBB", "CCCC"])
        service = self.service(client, recognizer)

        manual = service.search({"keywords": "LD-100", "status": 999, "page": 1, "page_size": 10})
        self.assertEqual(manual["verification"], {
            "status": "manual_required",
            "mode": "manual",
            "attempts": 3,
        })
        self.assertTrue(manual["captcha"]["image_data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(client.start_count, 4)

        verified = service.search({
            "keywords": "LD-100",
            "status": 999,
            "page": 1,
            "page_size": 10,
            "session_id": manual["session_id"],
            "captcha_code": "AB12",
        })
        self.assertEqual(verified["verification"], {
            "status": "verified",
            "mode": "manual",
            "attempts": 1,
        })

    def test_missing_ocr_falls_back_to_manual_and_refresh_does_not_query(self) -> None:
        client = FakeOrderClient()
        recognizer = FakeRecognizer([CaptchaRecognizerUnavailable("missing")])
        service = self.service(client, recognizer)

        manual = service.search({"keywords": "buyer", "status": 999, "page": 1, "page_size": 10})
        refreshed = service.search({
            "keywords": "buyer",
            "status": 999,
            "page": 1,
            "page_size": 10,
            "session_id": manual["session_id"],
            "refresh_captcha": True,
        })

        self.assertEqual(manual["verification"]["status"], "manual_required")
        self.assertEqual(manual["verification"]["attempts"], 0)
        self.assertEqual(refreshed["verification"], {
            "status": "manual_required",
            "mode": "manual",
            "attempts": 0,
        })
        self.assertEqual(client.list_calls, [])
        self.assertEqual(client.start_count, 3)

    def test_expired_ticket_is_reverified_within_three_attempt_budget(self) -> None:
        client = FakeOrderClient()
        client.expire_next_list = True
        service = self.service(client, FakeRecognizer(["AB12", "AB12"]))

        result = service.search({"keywords": "buyer", "status": 999, "page": 1, "page_size": 10})

        self.assertEqual(result["verification"], {
            "status": "verified",
            "mode": "ocr",
            "attempts": 2,
        })
        self.assertEqual(client.start_count, 2)
        self.assertEqual(len(client.list_calls), 2)

    def test_session_is_bound_to_keywords_and_manual_code_is_strict(self) -> None:
        client = FakeOrderClient()
        service = self.service(client, FakeRecognizer(["AB12"]))
        result = service.search({"keywords": "buyer-a", "status": 999, "page": 1, "page_size": 10})
        with self.assertRaisesRegex(OrderQueryInputError, "不匹配"):
            service.search({
                "keywords": "buyer-b",
                "status": 999,
                "page": 1,
                "page_size": 10,
                "session_id": result["session_id"],
            })
        with self.assertRaisesRegex(OrderQueryInputError, "验证码格式"):
            service.search({
                "keywords": "buyer-a",
                "status": 999,
                "page": 1,
                "page_size": 10,
                "session_id": result["session_id"],
                "captcha_code": "123",
            })


class RouteTests(unittest.TestCase):
    def test_manual_required_is_a_successful_route_result(self) -> None:
        responses: list[tuple[int, Any]] = []
        payload = {
            "session_id": "session",
            "expires_in": 100,
            "verification": {"status": "manual_required", "mode": "manual", "attempts": 3},
            "captcha": {"image_data_url": "data:image/png;base64,eA=="},
        }
        handled = routes.handle_post(
            "/api/order-query/search",
            {"keywords": "buyer"},
            send_json=lambda value, status=200: responses.append((status, value)),
            search=lambda data: payload,
        )
        self.assertTrue(handled)
        self.assertEqual(responses, [(200, payload)])

    def test_route_returns_structured_validation_error(self) -> None:
        responses: list[tuple[int, Any]] = []

        def fail(data: dict[str, Any]) -> dict[str, Any]:
            raise OrderQueryInputError("bad query")

        routes.handle_post(
            "/api/order-query/search",
            {},
            send_json=lambda value, status=200: responses.append((status, value)),
            search=fail,
        )
        self.assertEqual(responses, [(400, {
            "detail": "bad query",
            "code": "invalid_order_query",
            "retryable": False,
        })])

    def test_main_http_handler_mounts_order_query_route(self) -> None:
        body = json.dumps({
            "keywords": "buyer",
            "status": 999,
            "page": 1,
            "page_size": 10,
        }).encode("utf-8")
        expected = {
            "session_id": "session",
            "expires_in": 120,
            "verification": {"status": "verified", "mode": "ocr", "attempts": 1},
            "orders": [],
            "pagination": {"page": 1, "page_size": 10, "total": 0, "pages": 1},
        }
        trusted_origins = (
            ("http://127.0.0.1:5173/", "http://localhost:5188"),
            ("https://dashboard.example/app", "https://dashboard.example"),
        )
        for frontend_url, origin in trusted_origins:
            with self.subTest(origin=origin):
                handler = object.__new__(main.ApiHandler)
                handler.path = "/api/order-query/search"
                handler.headers = {
                    "Content-Length": str(len(body)),
                    "Content-Type": "application/json; charset=utf-8",
                    "Origin": origin,
                }
                handler.rfile = BytesIO(body)
                responses: list[tuple[int, Any]] = []
                handler._send_json = lambda value, status=200: responses.append((status, value))
                service = SimpleNamespace(search=lambda data: expected)

                with patch.object(main, "FRONTEND_URL", frontend_url), \
                     patch.object(main, "ORDER_QUERY_SERVICE", service):
                    handler.do_POST()

                self.assertEqual(responses, [(200, expected)])

    def test_order_query_request_guard_does_not_apply_to_other_routes(self) -> None:
        rejection = routes.request_rejection(
            "/api/preorders",
            {"Content-Type": "text/plain", "Origin": "https://untrusted.example"},
            frontend_url="http://127.0.0.1:5173/",
        )
        self.assertIsNone(rejection)

    def test_main_http_handler_rejects_order_query_with_wrong_content_type(self) -> None:
        body = json.dumps({"keywords": "buyer"}).encode("utf-8")
        handler = object.__new__(main.ApiHandler)
        handler.path = "/api/order-query/search"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "text/plain",
            "Origin": "http://127.0.0.1:5173",
        }
        handler.rfile = BytesIO(body)
        responses: list[tuple[int, Any]] = []
        handler._send_json = lambda value, status=200: responses.append((status, value))
        calls: list[dict[str, Any]] = []

        with patch.object(main, "ORDER_QUERY_SERVICE", SimpleNamespace(search=calls.append)):
            handler.do_POST()

        self.assertEqual(calls, [])
        self.assertEqual(responses, [(415, {
            "detail": "订单查询仅接受 application/json 请求",
            "code": "unsupported_media_type",
            "retryable": False,
        })])

    def test_main_http_handler_rejects_untrusted_order_query_origin(self) -> None:
        body = json.dumps({"keywords": "buyer"}).encode("utf-8")
        handler = object.__new__(main.ApiHandler)
        handler.path = "/api/order-query/search"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "Origin": "https://untrusted.example",
        }
        handler.rfile = BytesIO(body)
        responses: list[tuple[int, Any]] = []
        handler._send_json = lambda value, status=200: responses.append((status, value))
        calls: list[dict[str, Any]] = []

        with patch.object(main, "ORDER_QUERY_SERVICE", SimpleNamespace(search=calls.append)):
            handler.do_POST()

        self.assertEqual(calls, [])
        self.assertEqual(responses, [(403, {
            "detail": "订单查询请求来源不受信任",
            "code": "untrusted_origin",
            "retryable": False,
        })])


if __name__ == "__main__":
    unittest.main()
