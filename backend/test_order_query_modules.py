from __future__ import annotations

import base64
import json
import sys
import threading
import unittest
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.error import HTTPError
from urllib.request import Request

import main
from monitor_core.storefront import WafChallengeRequired
from order_query import routes
from order_query.captcha import CaptchaRecognizer, captcha_sign, normalize_captcha_code
from order_query.client import CaptchaChallenge, OrderQueryClient
from order_query.complaint import (
    COMPLAINT_PREVIEW_TARGET,
    COMPLAINT_REASONS,
    build_complaint_preview,
)
from order_query.detail import normalize_order_detail
from order_query.errors import (
    CaptchaRecognizerUnavailable,
    OrderComplaintInputError,
    OrderComplaintSubmissionConflict,
    OrderQueryDetailNotFound,
    OrderQueryInputError,
    OrderQueryPasswordInvalid,
    OrderQueryPasswordRateLimited,
    OrderQueryPasswordRequired,
    OrderQuerySessionExpired,
    OrderComplaintSubmissionUnknown,
    UpstreamOrderError,
)
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
        self.detail_calls: list[dict[str, Any]] = []
        self.detail_error: Exception | None = None
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
            "orders": [{
                "trade_no": "ORDER-1",
                "status": 1 if kwargs["status"] == 999 else kwargs["status"],
                "need_query_password": True,
                "goods_type": "card",
            }],
            "pagination": {
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
                "total": 1,
                "pages": 1,
            },
        }

    def get_order_detail(self, **kwargs: Any) -> dict[str, Any]:
        self.detail_calls.append(kwargs)
        if self.detail_error is not None:
            raise self.detail_error
        return {
            "trade_no": kwargs["trade_no"],
            "goods_type": "card",
            "status": 1,
            "delivery": {"kind": "card", "cards": ["CARD-SECRET"]},
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

    def test_default_recognizer_uses_legacy_shop_captcha_model(self) -> None:
        calls: list[dict[str, Any]] = []

        class DdddOcr:
            def __init__(self, **kwargs: Any) -> None:
                calls.append(kwargs)

            def classification(self, image: bytes) -> str:
                return "AB12"

        fake_module = SimpleNamespace(DdddOcr=DdddOcr)
        with patch.dict(sys.modules, {"ddddocr": fake_module}):
            recognizer = CaptchaRecognizer()
            self.assertEqual(recognizer.recognize(b"png"), "AB12")
        self.assertEqual(calls, [{"show_ad": False, "old": True}])

    def test_default_recognizer_falls_back_when_old_model_switch_is_unavailable(self) -> None:
        calls: list[dict[str, Any]] = []

        class DdddOcr:
            def __init__(self, **kwargs: Any) -> None:
                calls.append(kwargs)
                if "old" in kwargs:
                    raise TypeError("old is unsupported")

            def classification(self, image: bytes) -> str:
                return "CD34"

        fake_module = SimpleNamespace(DdddOcr=DdddOcr)
        with patch.dict(sys.modules, {"ddddocr": fake_module}):
            recognizer = CaptchaRecognizer()
            self.assertEqual(recognizer.recognize(b"png"), "CD34")
        self.assertEqual(calls, [{"show_ad": False, "old": True}, {"show_ad": False}])


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

    def test_http_403_aliyun_challenge_is_exposed_as_browser_verification(self) -> None:
        class WafOpener:
            def open(self, request: Request, timeout: float) -> Any:
                raise HTTPError(
                    request.full_url,
                    403,
                    "forbidden",
                    {"Content-Type": "text/html; charset=utf-8"},
                    BytesIO(b"<html>aliyun captcha challenge</html>"),
                )

        with self.assertRaises(WafChallengeRequired):
            OrderQueryClient(opener=WafOpener()).list_orders(
                keywords="buyer@example.test",
                ticket="ticket",
                status=999,
                page=1,
                page_size=10,
            )

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

    def test_complaint_history_uses_form_encoding_and_retains_visitor_id(self) -> None:
        opener = FakeOpener([
            self._json_response({"code": 1, "data": {"need_pwd": 1}}),
            self._json_response({
                "code": 1,
                "data": {
                    "status": 0,
                    "reason": "描述不符",
                    "content": "商品内容不一致",
                    "images": [],
                    "messages": [],
                    "collect_image": None,
                },
            }),
        ])
        client = OrderQueryClient(opener=opener)

        self.assertEqual(client.check_need_complaint_password(trade_no="ORDER-1"), {"need_pwd": 1})
        history = client.get_complaint_history(trade_no="ORDER-1", query_password="123456")
        self.assertEqual(history["status"], 0)
        self.assertEqual(len(opener.requests), 2)
        for request in opener.requests:
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertTrue(headers["content-type"].lower().startswith("application/x-www-form-urlencoded"))
            self.assertEqual(headers["visitorid"], client.visitor_id)
        self.assertEqual(parse_qs(opener.requests[0].data.decode("utf-8")), {"trade_no": ["ORDER-1"]})
        self.assertEqual(parse_qs(opener.requests[1].data.decode("utf-8")), {
            "trade_no": ["ORDER-1"],
            "query_pwd": ["123456"],
        })

    def test_complaint_password_check_rejects_missing_or_non_numeric_need_pwd(self) -> None:
        for value in (None, "1", True, 2, float("nan")):
            with self.subTest(value=value):
                data = {} if value is None else {"need_pwd": value}
                opener = FakeOpener([self._json_response({"code": 1, "data": data})])
                with self.assertRaisesRegex(UpstreamOrderError, "响应格式无效") as context:
                    OrderQueryClient(opener=opener).check_need_complaint_password(trade_no="ORDER-1")
                self.assertEqual(context.exception.code, "invalid_complaint_history_password_response")

    def test_complaint_history_maps_human_verification_failure_to_expired_session(self) -> None:
        for message in ("\u4eba\u673a\u9a8c\u8bc1\u5931\u8d25", "human verification failed"):
            with self.subTest(message=message):
                opener = FakeOpener([self._json_response({"code": 0, "msg": message, "data": None})])
                with self.assertRaises(OrderQuerySessionExpired):
                    OrderQueryClient(opener=opener).get_complaint_history(
                        trade_no="ORDER-1",
                        query_password="",
                    )

    def test_complaint_history_maps_missing_upstream_endpoint_to_distinct_error(self) -> None:
        opener = FakeOpener([self._json_response({
            "code": 0,
            "msg": "接口不存在",
            "data": None,
        })])
        with self.assertRaises(UpstreamOrderError) as context:
            OrderQueryClient(opener=opener).get_complaint_history(
                trade_no="ORDER-1",
                query_password="123456",
            )
        self.assertEqual(context.exception.code, "complaint_history_endpoint_unavailable")
        self.assertEqual(context.exception.status, 503)

    def test_complaint_password_check_maps_human_verification_failure_to_expired_session(self) -> None:
        opener = FakeOpener([self._json_response({
            "code": 0,
            "msg": "human verification failed",
            "data": None,
        })])
        with self.assertRaises(OrderQuerySessionExpired):
            OrderQueryClient(opener=opener).check_need_complaint_password(trade_no="ORDER-1")

    def test_order_detail_uses_fixed_endpoint_and_returns_a_strict_allowlist(self) -> None:
        upstream = {
            "code": 1,
            "msg": "success",
            "data": {
                "trade_no": "LD-DETAIL-1001",
                "transaction_id": "must-not-leak-transaction",
                "goods_name": "测试卡密商品",
                "quantity": 2,
                "sendout": 1,
                "total_amount": "46.13",
                "status": 1,
                "create_time": 1788056008,
                "success_time": 1788056027,
                "use_coupon": 1,
                "coupon_price": "2.5",
                "need_query_password": 1,
                "can_complaint": 1,
                "contact": "buyer@example.test",
                "private_contact": "must-not-leak-private",
                "user": {
                    "nickname": "测试商家",
                    "avatar": "https://pay.ldxp.cn/static/images/avatar.png",
                    "link": "https://pay.ldxp.cn/shop/SHOP123",
                    "contact_qq": "987654321",
                    "contact_mobile": "",
                    "contact_wechat": "merchant-wechat",
                    "token": "must-not-leak-token",
                },
                "goods": {
                    "goods_key": "GOODS1",
                    "goods_type": "card",
                    "link": "https://pay.ldxp.cn/item/GOODS1",
                    "extend": {
                        "instructions": (
                            '<p>使用 <a href="https://docs.example.com/start">文档</a>'
                            ' 和 [指南](https://guide.example.org/read)'
                            '、[危险](javascript:bad)</p>'
                            '<script>must-not-leak-script</script>'
                            '<a href="javascript:alert(1)">bad</a>'
                        ),
                    },
                },
                "response": {
                    "cards": ["CARD-A", "CARD-B"],
                    "export_cards_url": (
                        "https://pay.ldxp.cn/shopApi/Order/exportCards?trade_no=LD-DETAIL-1001"
                    ),
                    "api_status": 2,
                    "api_msg": "已发货",
                    "api_data": (
                        '<p>打开 [权益页](https://benefit.example.net/start)</p>'
                    ),
                },
            },
        }
        opener = FakeOpener([self._json_response(upstream)])
        client = OrderQueryClient(opener=opener)

        detail = client.get_order_detail(
            trade_no="LD-DETAIL-1001",
            query_password="test-password",
        )

        self.assertEqual(opener.requests[0].full_url, "https://pay.ldxp.cn/shopApi/Order/info")
        self.assertEqual(json.loads(opener.requests[0].data.decode("utf-8")), {
            "trade_no": "LD-DETAIL-1001",
            "query_password": "test-password",
            "dump": 1,
        })
        self.assertEqual(detail["total_amount"], "46.13")
        self.assertEqual(detail["success_at"], "2026-08-30T02:13:47+00:00")
        self.assertEqual(detail["contact"], "buyer@example.test")
        self.assertEqual(detail["seller"], {
            "nickname": "测试商家",
            "avatar": "https://pay.ldxp.cn/static/images/avatar.png",
            "shop_url": "https://pay.ldxp.cn/shop/SHOP123",
            "contact_qq": "987654321",
            "contact_mobile": "",
            "contact_wechat": "merchant-wechat",
        })
        self.assertEqual(detail["instructions"], {
            "text": "使用 文档 和 指南、危险\nbad",
            "links": [
                {"label": "文档", "url": "https://docs.example.com/start"},
                {"label": "指南", "url": "https://guide.example.org/read"},
            ],
        })
        self.assertEqual(detail["delivery"], {
            "kind": "card",
            "cards": ["CARD-A", "CARD-B"],
            "api_status": 2,
            "message": "已发货",
            "content": "打开 权益页",
            "links": [
                {"label": "权益页", "url": "https://benefit.example.net/start"},
            ],
            "truncated": False,
        })
        serialized = json.dumps(detail, ensure_ascii=False)
        for secret in (
            "must-not-leak-transaction",
            "must-not-leak-private",
            "must-not-leak-token",
            "must-not-leak-script",
            "exportCards",
            "javascript:",
        ):
            self.assertNotIn(secret, serialized)

    def test_order_detail_maps_password_and_session_errors_before_data_parsing(self) -> None:
        with self.assertRaises(OrderQueryPasswordInvalid):
            normalize_order_detail(
                {"code": 0, "msg": "安全密码错误，请使用联系方式重新查询后操作"},
                expected_trade_no="LD-DETAIL-1001",
            )
        with self.assertRaises(OrderQuerySessionExpired):
            normalize_order_detail(
                {"code": 0, "msg": "请使用联系方式重新查询后操作"},
                expected_trade_no="LD-DETAIL-1001",
            )

    def test_order_detail_rejects_a_mismatched_trade_number(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "不匹配"):
            normalize_order_detail(
                {"code": 1, "data": {"trade_no": "OTHER-ORDER", "status": 1}},
                expected_trade_no="LD-DETAIL-1001",
            )

    def test_equity_detail_flattens_bounded_structured_delivery_data(self) -> None:
        detail = normalize_order_detail(
            {
                "code": 1,
                "data": {
                    "trade_no": "LD-EQUITY-100",
                    "status": 1,
                    "goods": {
                        "goods_key": "EQUITY1",
                        "goods_type": "equity",
                        "extend": {},
                    },
                    "response": {
                        "api_status": 2,
                        "api_msg": "权益已发放",
                        "api_data": {
                            "account": {
                                "name": "buyer",
                                "portal": "[打开](https://benefit.example.net/account)",
                            },
                            "steps": [
                                "第一步",
                                '<a href="https://docs.example.com/equity">第二步</a>',
                            ],
                        },
                    },
                },
            },
            expected_trade_no="LD-EQUITY-100",
        )

        self.assertEqual(
            detail["delivery"]["content"],
            "account: name: buyer\n  portal: 打开\nsteps: 1: 第一步\n  2: 第二步",
        )
        self.assertEqual(detail["delivery"]["links"], [
            {"label": "打开", "url": "https://benefit.example.net/account"},
            {"label": "第二步", "url": "https://docs.example.com/equity"},
        ])
        self.assertFalse(detail["delivery"]["truncated"])

    def test_detail_links_reject_non_https_and_non_public_targets(self) -> None:
        detail = normalize_order_detail(
            {
                "code": 1,
                "data": {
                    "trade_no": "LD-DETAIL-LINKS",
                    "status": 1,
                    "user": {
                        "avatar": "https://cdn.example.com/avatar.png",
                        "link": "https://pay.ldxp.cn/shop/SHOP123",
                    },
                    "goods": {
                        "goods_type": "card",
                        "extend": {
                            "instructions": (
                                "[公开](https://docs.example.com/start) "
                                "[本机](https://127.0.0.1/admin) "
                                "[私网](https://10.0.0.8/secret) "
                                "[链路本地](https://169.254.169.254/latest) "
                                "[明文](http://docs.example.com/unsafe)"
                            ),
                        },
                    },
                    "response": {"cards": []},
                },
            },
            expected_trade_no="LD-DETAIL-LINKS",
        )

        self.assertEqual(detail["seller"]["avatar"], "")
        self.assertEqual(detail["seller"]["shop_url"], "https://pay.ldxp.cn/shop/SHOP123")
        self.assertEqual(detail["instructions"]["links"], [
            {"label": "公开", "url": "https://docs.example.com/start"},
        ])
        self.assertNotIn("127.0.0.1", json.dumps(detail, ensure_ascii=False))
        self.assertNotIn("169.254.169.254", json.dumps(detail, ensure_ascii=False))

    def test_complaint_context_normalizes_flags_and_rejects_mismatched_trade(self) -> None:
        opener = FakeOpener([
            self._json_response({
                "code": 1,
                "data": {
                    "trade_no": "ORDER-1",
                    "can_complaint": 1,
                    "complaint": None,
                },
            }),
        ])
        context = OrderQueryClient(opener=opener).get_complaint_context("ORDER-1")
        self.assertEqual(context, {
            "can_complaint": True,
            "complaint_status": -1,
        })
        self.assertTrue(getattr(context, "status_known"))
        self.assertEqual(len(opener.requests), 1)

        missing_status = FakeOpener([
            self._json_response({"code": 1, "data": {"trade_no": "ORDER-1", "can_complaint": 1}}),
        ])
        missing_context = OrderQueryClient(opener=missing_status).get_complaint_context("ORDER-1")
        self.assertEqual(missing_context["complaint_status"], -1)
        self.assertTrue(getattr(missing_context, "status_known"))

        top_level_status = FakeOpener([
            self._json_response({
                "code": 1,
                "data": {
                    "trade_no": "ORDER-1",
                    "can_complaint": 1,
                    "complaint_status": 0,
                },
            }),
        ])
        existing_context = OrderQueryClient(opener=top_level_status).get_complaint_context("ORDER-1")
        self.assertEqual(existing_context["complaint_status"], 0)
        self.assertTrue(getattr(existing_context, "status_known"))

        mismatch = FakeOpener([
            self._json_response({"code": 1, "data": {"trade_no": "OTHER", "can_complaint": 1}}),
        ])
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            OrderQueryClient(opener=mismatch).get_complaint_context("ORDER-1")

    def test_complaint_upload_is_multipart_file_and_requires_https_result(self) -> None:
        opener = FakeOpener([
            self._json_response({"code": 1, "data": {"url": "https://pay.ldxp.cn/uploads/a.png"}}),
        ])
        client = OrderQueryClient(opener=opener)
        url = client.upload_complaint_file(b"fixture", "a.png", "image/png")
        self.assertEqual(url, "https://pay.ldxp.cn/uploads/a.png")
        request = opener.requests[0]
        content_type = request.get_header("Content-type") or ""
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"', request.data)
        self.assertIn(b"fixture", request.data)

        insecure = FakeOpener([
            self._json_response({"code": 1, "data": {"url": "http://pay.ldxp.cn/uploads/a.png"}}),
        ])
        with self.assertRaisesRegex(RuntimeError, "有效地址"):
            OrderQueryClient(opener=insecure).upload_complaint_file(b"fixture", "a.png", "image/png")

    def test_complaint_writes_follow_official_nonzero_numeric_code_rules(self) -> None:
        opener = FakeOpener([
            self._json_response({"code": 2, "data": {"url": "https://pay.ldxp.cn/uploads/a.png"}}),
            self._json_response({"code": 2}),
        ])
        client = OrderQueryClient(opener=opener)
        self.assertEqual(
            client.upload_complaint_file(b"fixture", "a.png", "image/png"),
            "https://pay.ldxp.cn/uploads/a.png",
        )
        self.assertEqual(client.submit_complaint({"trade_no": "ORDER-1"})["code"], 2)

        failed_upload = FakeOpener([self._json_response({"code": 0, "msg": "failed"})])
        with self.assertRaisesRegex(RuntimeError, "failed"):
            OrderQueryClient(opener=failed_upload).upload_complaint_file(b"fixture", "a.png", "image/png")
        failed_submit = FakeOpener([self._json_response({"code": 0, "msg": "failed"})])
        with self.assertRaisesRegex(RuntimeError, "failed"):
            OrderQueryClient(opener=failed_submit).submit_complaint({"trade_no": "ORDER-1"})

        for malformed in ({}, {"code": "1"}, {"code": True}, {"code": float("nan")}, {"code": float("inf")}):
            with self.subTest(malformed=malformed):
                upload = FakeOpener([self._json_response({**malformed, "data": {"url": "https://pay.ldxp.cn/u/a.png"}})])
                with self.assertRaisesRegex(RuntimeError, "上传失败"):
                    OrderQueryClient(opener=upload).upload_complaint_file(b"fixture", "a.png", "image/png")
                submit = FakeOpener([self._json_response(malformed)])
                with self.assertRaisesRegex(RuntimeError, "响应格式无效"):
                    OrderQueryClient(opener=submit).submit_complaint({"trade_no": "ORDER-1"})


class ComplaintPreviewTests(unittest.TestCase):
    @staticmethod
    def valid_payload() -> dict[str, Any]:
        return {
            "trade_no": "LD260830R9AZU5",
            "reason": "描述不符",
            "content": "收到的卡密与商品描述不一致",
            "contact": "buyer@example.test",
            "images": [
                "https://pay.ldxp.cn/uploads/complaint/evidence-1.png",
                "http://cdn.example.test/evidence-2.jpg",
            ],
            "collect_image": "https://pay.ldxp.cn/uploads/complaint/refund.png",
            "query_pwd": "012345",
            "email_code": "",
        }

    def test_preview_normalizes_exact_official_payload_without_submitting(self) -> None:
        payload = self.valid_payload()
        payload.update({
            "trade_no": f"  {payload['trade_no']}  ",
            "reason": f" {payload['reason']} ",
            "content": f"\n{payload['content']}\n",
            "contact": f" {payload['contact']} ",
            "images": [f" {url} " for url in payload["images"]],
            "collect_image": f" {payload['collect_image']} ",
            "query_pwd": f" {payload['query_pwd']} ",
            "email_code": "",
        })

        result = build_complaint_preview(payload)

        self.assertFalse(result["submitted"])
        self.assertEqual(result["mode"], "preview")
        self.assertEqual(result["target"], {
            "method": "POST",
            "url": COMPLAINT_PREVIEW_TARGET,
        })
        self.assertEqual(list(result["payload"]), [
            "trade_no",
            "reason",
            "content",
            "contact",
            "images",
            "collect_image",
            "query_pwd",
            "email_code",
        ])
        self.assertEqual(result["payload"], self.valid_payload())

    def test_preview_accepts_every_official_complaint_reason(self) -> None:
        self.assertEqual(COMPLAINT_REASONS, {
            "不会使用",
            "无效商品",
            "涉嫌色情",
            "涉嫌赌博",
            "欺诈骗钱",
            "没人售后",
            "描述不符",
        })
        for reason in COMPLAINT_REASONS:
            with self.subTest(reason=reason):
                payload = self.valid_payload()
                payload["reason"] = reason
                self.assertEqual(build_complaint_preview(payload)["payload"]["reason"], reason)

    def test_preview_uses_empty_defaults_for_optional_official_fields(self) -> None:
        payload = self.valid_payload()
        for field in ("images", "collect_image", "email_code"):
            payload.pop(field)

        result = build_complaint_preview(payload)

        self.assertEqual(result["payload"]["images"], [])
        self.assertEqual(result["payload"]["collect_image"], "")
        self.assertEqual(result["payload"]["email_code"], "")

    def test_preview_rejects_invalid_or_unofficial_fields(self) -> None:
        invalid_payloads: list[tuple[str, Any, str]] = [
            ("body", [], "JSON 对象"),
            ("unknown", {**self.valid_payload(), "target": "https://example.test"}, "未知字段"),
            ("trade_no", {**self.valid_payload(), "trade_no": "LD/../../bad"}, "trade_no 格式"),
            ("reason", {**self.valid_payload(), "reason": "其他"}, "投诉类型"),
            ("empty_content", {**self.valid_payload(), "content": "  "}, "content 不能为空"),
            ("long_content", {**self.valid_payload(), "content": "x" * 201}, "最多允许 200"),
            ("contact", {**self.valid_payload(), "contact": "not-an-email"}, "邮箱地址"),
            ("password", {**self.valid_payload(), "query_pwd": "12345"}, "6 位数字"),
            ("unicode_password", {**self.valid_payload(), "query_pwd": "１２３４５６"}, "6 位数字"),
            ("email_code", {**self.valid_payload(), "email_code": "A1B2"}, "不使用邮箱验证码"),
            ("images_type", {**self.valid_payload(), "images": "https://example.test/a.png"}, "URL 数组"),
            ("images_count", {**self.valid_payload(), "images": ["https://example.test/a.png"] * 4}, "最多允许 3"),
            ("image_url", {**self.valid_payload(), "images": ["file:///tmp/a.png"]}, r"http\(s\) URL"),
            ("malformed_image_url", {**self.valid_payload(), "images": ["http://[invalid"]}, r"http\(s\) URL"),
            ("collect_image", {**self.valid_payload(), "collect_image": "ftp://example.test/refund.png"}, r"http\(s\) URL"),
        ]
        for label, payload, message in invalid_payloads:
            with self.subTest(label=label), self.assertRaisesRegex(OrderComplaintInputError, message):
                build_complaint_preview(payload)


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

    def test_detail_reuses_verified_cookie_client_and_does_not_cache_password(self) -> None:
        client = FakeOrderClient()
        service = self.service(client, FakeRecognizer(["AB12"]))
        search = service.search({
            "keywords": "buyer",
            "status": 999,
            "page": 1,
            "page_size": 10,
        })

        result = service.detail({
            "keywords": "buyer",
            "session_id": search["session_id"],
            "trade_no": "ORDER-1",
            "query_password": "secret-password",
        })

        self.assertEqual(result["detail"]["trade_no"], "ORDER-1")
        self.assertEqual(client.detail_calls, [{
            "trade_no": "ORDER-1",
            "query_password": "secret-password",
        }])
        session = service.sessions.get(search["session_id"], "buyer")
        self.assertNotIn("query_password", session.__dict__)
        self.assertNotIn("secret-password", repr(session.cache))

    def test_detail_requires_password_and_rejects_orders_not_seen_in_the_session(self) -> None:
        client = FakeOrderClient()
        service = self.service(client, FakeRecognizer(["AB12"]))
        search = service.search({
            "keywords": "buyer",
            "status": 999,
            "page": 1,
            "page_size": 10,
        })
        base = {
            "keywords": "buyer",
            "session_id": search["session_id"],
            "query_password": "",
        }

        with self.assertRaises(OrderQueryPasswordRequired):
            service.detail({**base, "trade_no": "ORDER-1"})
        with self.assertRaises(OrderQueryDetailNotFound):
            service.detail({
                **base,
                "trade_no": "OTHER-ORDER",
                "query_password": "secret-password",
            })
        self.assertEqual(client.detail_calls, [])

    def test_upstream_detail_session_expiry_clears_local_authorization(self) -> None:
        client = FakeOrderClient()
        service = self.service(client, FakeRecognizer(["AB12"]))
        search = service.search({
            "keywords": "buyer",
            "status": 999,
            "page": 1,
            "page_size": 10,
        })
        client.detail_error = OrderQuerySessionExpired()

        with self.assertRaises(OrderQuerySessionExpired):
            service.detail({
                "keywords": "buyer",
                "session_id": search["session_id"],
                "trade_no": "ORDER-1",
                "query_password": "secret-password",
            })

        session = service.sessions.get(search["session_id"], "buyer")
        self.assertEqual(session.ticket, "")
        self.assertEqual(session.authorized_orders, {})

    def test_five_invalid_passwords_apply_per_order_backoff(self) -> None:
        client = FakeOrderClient()
        service = self.service(client, FakeRecognizer(["AB12"]))
        search = service.search({
            "keywords": "buyer",
            "status": 999,
            "page": 1,
            "page_size": 10,
        })
        client.detail_error = OrderQueryPasswordInvalid()
        request = {
            "keywords": "buyer",
            "session_id": search["session_id"],
            "trade_no": "ORDER-1",
            "query_password": "wrong-password",
        }

        for _ in range(4):
            with self.assertRaises(OrderQueryPasswordInvalid):
                service.detail(request)
        with self.assertRaises(OrderQueryPasswordRateLimited):
            service.detail(request)
        with self.assertRaises(OrderQueryPasswordRateLimited):
            service.detail(request)

        self.assertEqual(len(client.detail_calls), 5)
        session = service.sessions.get(search["session_id"], "buyer")
        self.assertEqual(session.password_failures["ORDER-1"][0], 5)

    def test_successful_password_clears_previous_failure_count(self) -> None:
        client = FakeOrderClient()
        service = self.service(client, FakeRecognizer(["AB12"]))
        search = service.search({
            "keywords": "buyer",
            "status": 999,
            "page": 1,
            "page_size": 10,
        })
        request = {
            "keywords": "buyer",
            "session_id": search["session_id"],
            "trade_no": "ORDER-1",
            "query_password": "password",
        }
        client.detail_error = OrderQueryPasswordInvalid()
        with self.assertRaises(OrderQueryPasswordInvalid):
            service.detail(request)
        session = service.sessions.get(search["session_id"], "buyer")
        self.assertEqual(session.password_failures["ORDER-1"][0], 1)

        client.detail_error = None
        service.detail(request)

        self.assertNotIn("ORDER-1", session.password_failures)

    def test_password_backoff_expires_and_verification_clear_removes_failures(self) -> None:
        clock = [100.0]
        store = OrderQuerySessionStore(
            ttl_seconds=120,
            cache_seconds=0,
            clock=lambda: clock[0],
        )
        session = store.create("buyer", FakeOrderClient())
        for _ in range(4):
            self.assertFalse(session.record_password_failure("ORDER-1", store.now()))
        self.assertTrue(session.record_password_failure("ORDER-1", store.now()))
        self.assertFalse(session.password_attempt_allowed("ORDER-1", store.now()))

        clock[0] += 61
        self.assertTrue(session.password_attempt_allowed("ORDER-1", store.now()))
        self.assertNotIn("ORDER-1", session.password_failures)

        session.record_password_failure("ORDER-1", store.now())
        session.clear_verification()
        self.assertEqual(session.password_failures, {})

    def test_detail_rejects_non_string_or_control_character_passwords(self) -> None:
        for password in (987654, "secret\npassword", "x" * 161):
            with self.subTest(password_type=type(password).__name__):
                with self.assertRaises(OrderQueryInputError):
                    OrderQueryService._validated_detail_request({
                        "keywords": "buyer",
                        "session_id": "abcdefghijklmnop",
                        "trade_no": "ORDER-1",
                        "query_password": password,
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
            detail=lambda data: {},
            complaint_preview=lambda data: {},
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
            detail=lambda data: {},
            complaint_preview=lambda data: {},
        )
        self.assertEqual(responses, [(400, {
            "detail": "bad query",
            "code": "invalid_order_query",
            "retryable": False,
        })])

    def test_missing_history_operation_returns_distinct_service_error(self) -> None:
        responses: list[tuple[int, Any]] = []
        handled = routes.handle_post(
            routes.ORDER_COMPLAINT_HISTORY_PATH,
            {},
            send_json=lambda value, status=200: responses.append((status, value)),
            search=lambda data: {},
            detail=lambda data: {},
            complaint_preview=lambda data: {},
        )
        self.assertTrue(handled)
        self.assertEqual(responses, [(503, {
            "detail": "售后记录接口未启用，请重启后端服务",
            "code": "order_complaint_history_unavailable",
            "retryable": True,
        })])

    def test_detail_route_returns_a_specific_password_error(self) -> None:
        responses: list[tuple[int, Any]] = []

        def fail(data: dict[str, Any]) -> dict[str, Any]:
            raise OrderQueryPasswordInvalid()

        handled = routes.handle_post(
            "/api/order-query/detail",
            {"trade_no": "ORDER-1"},
            send_json=lambda value, status=200: responses.append((status, value)),
            search=lambda data: {},
            detail=fail,
            complaint_preview=lambda data: {},
        )

        self.assertTrue(handled)
        self.assertEqual(responses, [(403, {
            "detail": "订单安全密码错误，请重新输入",
            "code": "order_query_password_invalid",
            "retryable": False,
        })])

    def test_complaint_preview_route_is_distinct_and_returns_local_result(self) -> None:
        responses: list[tuple[int, Any]] = []
        calls: list[dict[str, Any]] = []
        payload = ComplaintPreviewTests.valid_payload()
        expected = build_complaint_preview(payload)

        handled = routes.handle_post(
            "/api/order-query/complaints/preview",
            payload,
            send_json=lambda value, status=200: responses.append((status, value)),
            search=lambda data: self.fail("search route must not run"),
            detail=lambda data: self.fail("detail route must not run"),
            complaint_preview=lambda data: calls.append(data) or expected,
        )

        self.assertTrue(handled)
        self.assertEqual(calls, [payload])
        self.assertEqual(responses, [(200, expected)])

    def test_complaint_preview_route_returns_specific_validation_error(self) -> None:
        responses: list[tuple[int, Any]] = []

        def fail(data: dict[str, Any]) -> dict[str, Any]:
            raise OrderComplaintInputError("bad complaint")

        routes.handle_post(
            "/api/order-query/complaints/preview",
            {},
            send_json=lambda value, status=200: responses.append((status, value)),
            search=lambda data: {},
            detail=lambda data: {},
            complaint_preview=fail,
        )

        self.assertEqual(responses, [(400, {
            "detail": "bad complaint",
            "code": "invalid_order_complaint",
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
                service = SimpleNamespace(search=lambda data: expected, detail=lambda data: {})

                with patch.object(main, "FRONTEND_URL", frontend_url), \
                     patch.object(main, "ORDER_QUERY_SERVICE", service):
                    handler.do_POST()

                self.assertEqual(responses, [(200, expected)])

    def test_main_http_handler_mounts_order_detail_route(self) -> None:
        body = json.dumps({
            "keywords": "buyer",
            "session_id": "abcdefghijklmnop",
            "trade_no": "ORDER-1",
            "query_password": "secret-password",
        }).encode("utf-8")
        expected = {
            "session_id": "abcdefghijklmnop",
            "expires_in": 120,
            "detail": {"trade_no": "ORDER-1", "delivery": {"cards": ["CARD"]}},
        }
        handler = object.__new__(main.ApiHandler)
        handler.path = "/api/order-query/detail"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:5173",
        }
        handler.rfile = BytesIO(body)
        responses: list[tuple[int, Any]] = []
        handler._send_json = lambda value, status=200: responses.append((status, value))
        calls: list[dict[str, Any]] = []
        service = SimpleNamespace(
            search=lambda data: {},
            detail=lambda data: calls.append(data) or expected,
        )

        with patch.object(main, "ORDER_QUERY_SERVICE", service):
            handler.do_POST()

        self.assertEqual(calls, [json.loads(body)])
        self.assertEqual(responses, [(200, expected)])

    def test_main_http_handler_mounts_local_complaint_preview_without_network(self) -> None:
        payload = ComplaintPreviewTests.valid_payload()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        expected = build_complaint_preview(payload)
        handler = object.__new__(main.ApiHandler)
        handler.path = "/api/order-query/complaints/preview"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json; charset=utf-8",
            "Origin": "http://127.0.0.1:5173",
        }
        handler.rfile = BytesIO(body)
        responses: list[tuple[int, Any]] = []
        handler._send_json = lambda value, status=200: responses.append((status, value))

        with patch.object(main, "build_complaint_preview", wraps=build_complaint_preview) as preview, \
             patch.object(OrderQueryClient, "_json_request", side_effect=AssertionError("network called")):
            handler.do_POST()

        preview.assert_called_once_with(payload)
        self.assertEqual(responses, [(200, expected)])

    def test_main_http_handler_mounts_all_complaint_workflow_routes(self) -> None:
        endpoints = {
            "/api/order-query/complaints/context": "complaint_context",
            "/api/order-query/complaints/history": "complaint_history",
            "/api/order-query/complaints/upload": "complaint_upload",
            "/api/order-query/complaints/upload/remove": "complaint_remove_upload",
            "/api/order-query/complaints/submit": "complaint_submit",
        }
        self.assertNotIn("/api/order-query/complaints/email-code", routes.ORDER_QUERY_POST_PATHS)
        for path, method_name in endpoints.items():
            with self.subTest(path=path):
                body = b"{}"
                handler = object.__new__(main.ApiHandler)
                handler.path = path
                handler.headers = {
                    "Content-Length": str(len(body)),
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1:5173",
                }
                handler.rfile = BytesIO(body)
                responses: list[tuple[int, Any]] = []
                calls: list[dict[str, Any]] = []
                handler._send_json = lambda value, status=200: responses.append((status, value))
                service = SimpleNamespace(search=lambda data: {}, detail=lambda data: {})
                setattr(service, method_name, lambda data, name=method_name: calls.append(data) or {"route": name})

                with patch.object(main, "ORDER_QUERY_SERVICE", service):
                    handler.do_POST()

                self.assertEqual(calls, [{}])
                self.assertEqual(responses, [(200, {"route": method_name})])

    def test_complaint_preview_request_guard_rejects_bad_content_type_and_origin(self) -> None:
        cases = (
            (
                {"Content-Type": "text/plain", "Origin": "http://127.0.0.1:5173"},
                415,
                "unsupported_media_type",
            ),
            (
                {"Content-Type": "application/json", "Origin": "https://untrusted.example"},
                403,
                "untrusted_origin",
            ),
        )
        for headers, expected_status, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                rejection = routes.request_rejection(
                    "/api/order-query/complaints/preview",
                    headers,
                    frontend_url="http://127.0.0.1:5173/",
                )
                self.assertIsNotNone(rejection)
                assert rejection is not None
                status, response = rejection
                self.assertEqual(status, expected_status)
                self.assertEqual(response["code"], expected_code)

    def test_order_query_request_guard_does_not_apply_to_other_routes(self) -> None:
        rejection = routes.request_rejection(
            "/api/preorders",
            {"Content-Type": "text/plain", "Origin": "https://untrusted.example"},
            frontend_url="http://127.0.0.1:5173/",
        )
        self.assertIsNone(rejection)

    def test_order_detail_request_guard_requires_json_and_a_trusted_origin(self) -> None:
        unsupported = routes.request_rejection(
            "/api/order-query/detail",
            {"Content-Type": "text/plain", "Origin": "http://127.0.0.1:5173"},
            frontend_url="http://127.0.0.1:5173/",
        )
        untrusted = routes.request_rejection(
            "/api/order-query/detail",
            {"Content-Type": "application/json", "Origin": "https://untrusted.example"},
            frontend_url="http://127.0.0.1:5173/",
        )

        self.assertEqual(unsupported[0], 415)
        self.assertEqual(untrusted[0], 403)

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

        with patch.object(
            main,
            "ORDER_QUERY_SERVICE",
            SimpleNamespace(search=calls.append, detail=calls.append),
        ):
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

        with patch.object(
            main,
            "ORDER_QUERY_SERVICE",
            SimpleNamespace(search=calls.append, detail=calls.append),
        ):
            handler.do_POST()

        self.assertEqual(calls, [])
        self.assertEqual(responses, [(403, {
            "detail": "订单查询请求来源不受信任",
            "code": "untrusted_origin",
            "retryable": False,
        })])


class ComplaintWorkflowTests(unittest.TestCase):
    class Client(FakeOrderClient):
        def __init__(self, *, submit_error: Exception | None = None, status_known: bool = True) -> None:
            super().__init__()
            self.submit_error = submit_error
            self.status_known = status_known
            self.context_calls: list[str] = []
            self.upload_calls: list[dict[str, Any]] = []
            self.submit_calls: list[dict[str, Any]] = []

        def list_orders(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "orders": [{"trade_no": "ORDER-1", "status": 1, "need_query_password": False}],
                "pagination": {"page": 1, "page_size": 10, "total": 1, "pages": 1},
            }

        def get_complaint_context(self, *, trade_no: str) -> dict[str, Any]:
            self.context_calls.append(trade_no)
            result = {
                "can_complaint": True,
                "complaint_status": -1,
            }
            if self.status_known:
                result["complaint_status_known"] = True
            return result

        def upload_complaint_file(self, **kwargs: Any) -> str:
            self.upload_calls.append(kwargs)
            return f"https://pay.ldxp.cn/uploads/{len(self.upload_calls)}.png"

        def submit_complaint(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.submit_calls.append(payload)
            if self.submit_error is not None:
                raise self.submit_error
            return {"code": 1}

    @staticmethod
    def _identity(service: OrderQueryService) -> dict[str, str]:
        result = service.search({"keywords": "buyer"})
        return {"keywords": "buyer", "session_id": result["session_id"], "trade_no": "ORDER-1"}

    @staticmethod
    def _service(client: Any, recognizer: Any = None) -> OrderQueryService:
        return OrderQueryService(
            client_factory=lambda: client,
            recognizer=recognizer or FakeRecognizer(["AB12"]),
            sessions=OrderQuerySessionStore(ttl_seconds=120, cache_seconds=0),
        )

    def test_context_upload_and_submit_use_authorized_session_and_exact_payload(self) -> None:
        client = self.Client()
        service = self._service(client)
        identity = self._identity(service)
        context_response = service.complaint_context(identity)
        self.assertIs(context_response["complaint_status_known"], True)
        image = b"\x89PNG\r\n\x1a\nfixture"
        uploaded = service.complaint_upload({
            **identity,
            "name": "evidence.png",
            "mime_type": "image/png",
            "data_base64": base64.b64encode(image).decode("ascii"),
        })
        reason = next(iter(COMPLAINT_REASONS))
        payload = {
            **identity,
            "reason": reason,
            "content": "商品描述与收到内容不一致",
            "contact": "buyer@example.test",
            "images": [uploaded["url"]],
            "collect_image": "",
            "query_pwd": "012345",
            "email_code": "",
        }
        result = service.complaint_submit(payload)
        self.assertEqual(result["submitted"], True)
        self.assertEqual(client.submit_calls, [{
            "trade_no": "ORDER-1",
            "reason": reason,
            "content": "商品描述与收到内容不一致",
            "contact": "buyer@example.test",
            "images": [uploaded["url"]],
            "collect_image": "",
            "query_pwd": "012345",
            "email_code": "",
        }])
        self.assertEqual(client.context_calls, ["ORDER-1", "ORDER-1"])
        with self.assertRaisesRegex(Exception, "重复提交"):
            service.complaint_submit(payload)
        self.assertEqual(len(client.submit_calls), 1)

    def test_existing_top_level_complaint_status_blocks_submit(self) -> None:
        class ExistingComplaintClient(self.Client):
            def get_complaint_context(self, *, trade_no: str) -> dict[str, Any]:
                self.context_calls.append(trade_no)
                return {
                    "can_complaint": True,
                    "complaint_status": 0,
                    "complaint_status_known": True,
                }

        client = ExistingComplaintClient()
        service = self._service(client)
        identity = self._identity(service)
        with self.assertRaises(OrderComplaintSubmissionConflict):
            service.complaint_submit(identity)
        self.assertEqual(client.context_calls, ["ORDER-1"])
        self.assertEqual(client.submit_calls, [])

    def test_unknown_official_status_blocks_upload_and_submit(self) -> None:
        client = self.Client(status_known=False)
        service = self._service(client)
        identity = self._identity(service)
        image = b"\x89PNG\r\n\x1a\nfixture"
        with self.assertRaisesRegex(RuntimeError, "状态无法确认"):
            service.complaint_upload({
                **identity,
                "name": "evidence.png",
                "mime_type": "image/png",
                "data_base64": base64.b64encode(image).decode("ascii"),
            })
        with self.assertRaisesRegex(RuntimeError, "状态无法确认"):
            service.complaint_submit({
                **identity,
                "reason": next(iter(COMPLAINT_REASONS)),
                "content": "x",
                "contact": "buyer@example.test",
                "images": [],
                "collect_image": "",
                "query_pwd": "012345",
                "email_code": "",
            })
        self.assertEqual(client.upload_calls, [])
        self.assertEqual(client.submit_calls, [])

    def test_two_sessions_cannot_submit_the_same_trade_concurrently(self) -> None:
        context_barrier = threading.Barrier(2)

        class RacingClient(self.Client):
            def get_complaint_context(self, *, trade_no: str) -> dict[str, Any]:
                context_barrier.wait(2)
                return super().get_complaint_context(trade_no=trade_no)

        client = RacingClient()
        service = self._service(client, FakeRecognizer(["AB12", "AB12"]))
        first_identity = self._identity(service)
        second_identity = self._identity(service)
        reason = next(iter(COMPLAINT_REASONS))

        def payload(identity: dict[str, str]) -> dict[str, Any]:
            return {
                **identity,
                "reason": reason,
                "content": "x",
                "contact": "buyer@example.test",
                "images": [],
                "collect_image": "",
                "query_pwd": "012345",
                "email_code": "",
            }

        results: list[dict[str, Any]] = []
        errors: list[Exception] = []

        def submit(identity: dict[str, str]) -> None:
            try:
                results.append(service.complaint_submit(payload(identity)))
            except Exception as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=submit, args=(first_identity,)),
            threading.Thread(target=submit, args=(second_identity,)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OrderComplaintSubmissionConflict)
        self.assertEqual(len(client.submit_calls), 1)

    def test_unknown_submit_is_locked_until_context_refresh(self) -> None:
        client = self.Client(submit_error=RuntimeError("network"))
        service = self._service(client)
        identity = self._identity(service)
        image = b"\x89PNG\r\n\x1a\nfixture"
        uploaded = service.complaint_upload({
            **identity,
            "name": "evidence.png",
            "mime_type": "image/png",
            "data_base64": base64.b64encode(image).decode("ascii"),
        })
        payload = {
            **identity,
            "reason": next(iter(COMPLAINT_REASONS)),
            "content": "x",
            "contact": "buyer@example.test",
            "images": [uploaded["url"]],
            "collect_image": "",
            "query_pwd": "012345",
            "email_code": "",
        }
        with self.assertRaises(OrderComplaintSubmissionUnknown):
            service.complaint_submit(payload)
        with self.assertRaises(OrderComplaintSubmissionUnknown):
            service.complaint_submit(payload)
        self.assertEqual(len(client.submit_calls), 1)
        service.complaint_context(identity)
        with self.assertRaises(OrderComplaintSubmissionUnknown):
            service.complaint_submit(payload)
        self.assertEqual(len(client.submit_calls), 2)

    def test_unparseable_submit_responses_lock_without_a_second_request(self) -> None:
        for code in ("invalid_order_response", "upstream_response_too_large"):
            with self.subTest(code=code):
                client = self.Client(submit_error=UpstreamOrderError("bad response", code=code))
                service = self._service(client)
                identity = self._identity(service)
                payload = {
                    **identity,
                    "reason": next(iter(COMPLAINT_REASONS)),
                    "content": "x",
                    "contact": "buyer@example.test",
                    "images": [],
                    "collect_image": "",
                    "query_pwd": "012345",
                    "email_code": "",
                }
                with self.assertRaises(OrderComplaintSubmissionUnknown):
                    service.complaint_submit(payload)
                with self.assertRaises(OrderComplaintSubmissionUnknown):
                    service.complaint_submit(payload)
                self.assertEqual(len(client.submit_calls), 1)

    def test_unknown_submit_is_not_cleared_by_context_without_explicit_status(self) -> None:
        client = self.Client(submit_error=RuntimeError("network"))
        service = self._service(client)
        identity = self._identity(service)
        image = b"\x89PNG\r\n\x1a\nfixture"
        uploaded = service.complaint_upload({
            **identity,
            "name": "evidence.png",
            "mime_type": "image/png",
            "data_base64": base64.b64encode(image).decode("ascii"),
        })
        payload = {
            **identity,
            "reason": next(iter(COMPLAINT_REASONS)),
            "content": "x",
            "contact": "buyer@example.test",
            "images": [uploaded["url"]],
            "collect_image": "",
            "query_pwd": "012345",
            "email_code": "",
        }
        with self.assertRaises(OrderComplaintSubmissionUnknown):
            service.complaint_submit(payload)
        client.status_known = False
        context_response = service.complaint_context(identity)
        self.assertIs(context_response["complaint_status_known"], False)
        with self.assertRaises(OrderComplaintSubmissionUnknown):
            service.complaint_submit(payload)
        self.assertEqual(len(client.submit_calls), 1)

    def test_complaint_history_accepts_official_password_text_and_returns_normalized_record(self) -> None:
        class HistoryClient(self.Client):
            def __init__(self) -> None:
                super().__init__()
                self.history_passwords: list[str] = []

            def check_need_complaint_password(self, *, trade_no: str) -> dict[str, Any]:
                self.context_calls.append(trade_no)
                return {"need_pwd": 1}

            def get_complaint_history(self, *, trade_no: str, query_password: str = "") -> dict[str, Any]:
                self.history_passwords.append(query_password)
                return {
                    "status": 1,
                    "reason": "描述不符",
                    "content": "已协商完成",
                    "images": [],
                    "contact": "buyer@example.test",
                    "create_time": 1788134400,
                    "messages": [{
                        "identity": "platform",
                        "content_type": 0,
                        "content": "平台已处理",
                        "create_time": 1_788_134_400,
                    }],
                    "collect_image": None,
                }

        client = HistoryClient()
        service = self._service(client)
        identity = self._identity(service)

        with self.assertRaises(OrderQueryPasswordRequired):
            service.complaint_history({**identity, "query_password": ""})
        for password in ("bad\npassword", "x" * 161):
            with self.subTest(password=password):
                with self.assertRaises(OrderComplaintInputError):
                    service.complaint_history({**identity, "query_password": password})
        result = service.complaint_history({**identity, "query_password": "123456"})
        self.assertEqual(result["need_query_password"], True)
        self.assertEqual(result["complaint"]["status"], 1)
        self.assertEqual(result["complaint"]["messages"][0]["identity"], "platform")
        self.assertEqual(result["complaint"]["created_at"], "2026-08-31T00:00:00+00:00")
        self.assertEqual(result["complaint"]["messages"][0]["created_at"], "2026-08-31T00:00:00+00:00")
        self.assertEqual(client.history_passwords, ["123456"])

    def test_complaint_history_check_fails_closed_when_need_pwd_is_malformed(self) -> None:
        class MalformedHistoryClient(self.Client):
            def __init__(self, value: Any) -> None:
                super().__init__()
                self.value = value

            def check_need_complaint_password(self, *, trade_no: str) -> dict[str, Any]:
                return {"need_pwd": self.value}

            def get_complaint_history(self, *, trade_no: str, query_password: str = "") -> dict[str, Any]:
                raise AssertionError("history must not be requested after malformed password metadata")

        identity_service = self._service(MalformedHistoryClient(None))
        identity = self._identity(identity_service)
        with self.assertRaises(UpstreamOrderError) as context:
            identity_service.complaint_history({**identity, "query_password": ""})
        self.assertEqual(context.exception.code, "invalid_complaint_history_password_response")

    def test_complaint_history_rate_limits_explicit_password_errors_when_metadata_says_public(self) -> None:
        class InconsistentHistoryClient(self.Client):
            def __init__(self) -> None:
                super().__init__()
                self.history_calls = 0

            def check_need_complaint_password(self, *, trade_no: str) -> dict[str, Any]:
                return {"need_pwd": 0}

            def get_complaint_history(self, *, trade_no: str, query_password: str = "") -> dict[str, Any]:
                self.history_calls += 1
                raise OrderQueryPasswordInvalid()

        client = InconsistentHistoryClient()
        service = self._service(client)
        identity = self._identity(service)

        for attempt in range(4):
            with self.subTest(attempt=attempt + 1):
                with self.assertRaises(OrderQueryPasswordInvalid):
                    service.complaint_history({**identity, "query_password": ""})
        with self.assertRaises(OrderQueryPasswordRateLimited):
            service.complaint_history({**identity, "query_password": ""})
        with self.assertRaises(OrderQueryPasswordRateLimited):
            service.complaint_history({**identity, "query_password": ""})
        self.assertEqual(client.history_calls, 5)

    def test_public_complaint_history_does_not_clear_detail_password_failures(self) -> None:
        class PublicHistoryClient(self.Client):
            def check_need_complaint_password(self, *, trade_no: str) -> dict[str, Any]:
                return {"need_pwd": 0}

            def get_complaint_history(self, *, trade_no: str, query_password: str = "") -> dict[str, Any]:
                return {
                    "status": 0,
                    "reason": "鎻忚堪涓嶇",
                    "content": "x",
                    "images": [],
                    "messages": [],
                    "collect_image": None,
                }

        client = PublicHistoryClient()
        service = self._service(client)
        identity = self._identity(service)
        session = service.sessions.get(identity["session_id"], identity["keywords"])
        session.record_password_failure(identity["trade_no"], service.sessions.now())

        result = service.complaint_history({**identity, "query_password": ""})

        self.assertEqual(result["complaint"]["status"], 0)
        self.assertIn(identity["trade_no"], session.password_failures)
        self.assertNotIn(identity["trade_no"], session.complaint_password_failures)


if __name__ == "__main__":
    unittest.main()
