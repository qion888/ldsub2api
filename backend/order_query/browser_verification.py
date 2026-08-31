"""Dedicated browser session for order-query WAF challenges.

The monitor and order lookup flows intentionally use separate managers and
profiles.  A monitor verification may be running while a buyer is solving an
order lookup challenge; sharing the driver would mix cookies and requests.
"""

from __future__ import annotations

import json
import threading
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

# Keep imports explicit here instead of importing the service module (which
# would create a service/manager cycle during application startup).
from monitor_core.storefront import ALLOWED_HOST, WAF_MARKERS, WafChallengeRequired, is_waf_response

from .client import BASE_URL, normalize_order_list
from .errors import OrderQueryInputError, OrderQuerySessionExpired


class OrderQueryBrowserVerificationManager:
    """Open Edge, let the user solve Aliyun WAF, then replay one order query."""

    def __init__(
        self,
        *,
        sessions: Any,
        waf_markers: tuple[bytes, ...] = WAF_MARKERS,
        profile_path: Path,
    ) -> None:
        self._sessions = sessions
        self._waf_markers = waf_markers
        self._profile_path = profile_path
        self.lock = threading.Lock()
        self.driver: Any = None
        self.session_id: str | None = None
        self.keywords: str | None = None
        self.request: dict[str, int] | None = None

    @staticmethod
    def _validated_request(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise OrderQueryInputError("WAF 验证参数必须是 JSON 对象")
        keywords = str(data.get("keywords") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        if not keywords or len(keywords) > 160:
            raise OrderQueryInputError("WAF 验证缺少有效的查询内容")
        if not session_id or len(session_id) < 16 or len(session_id) > 128:
            raise OrderQueryInputError("WAF 验证缺少有效的查询会话")
        try:
            status = int(data.get("status", 999))
            page = int(data.get("page", 1))
            page_size = int(data.get("page_size", 10))
        except (TypeError, ValueError) as exc:
            raise OrderQueryInputError("WAF 验证的订单分页参数无效") from exc
        if status not in {999, 0, 1, 2, 3}:
            raise OrderQueryInputError("WAF 验证的订单状态筛选无效")
        if page < 1 or page > 1000 or page_size < 1 or page_size > 50:
            raise OrderQueryInputError("WAF 验证的订单分页参数超出范围")
        return {
            "session_id": session_id,
            "keywords": keywords,
            "status": status,
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    def _payload(session: Any, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": request["status"],
            "current": request["page"],
            "pageSize": request["page_size"],
            "keywords": request["keywords"],
            "ticket": session.ticket,
        }

    def _is_waf_html(self, text: str) -> bool:
        raw = str(text or "").encode("utf-8", "ignore")
        # A normal /order document is also HTML; only challenge signatures
        # may trigger the early page-source check.
        lowered = raw.lower()
        return any(marker.lower() in lowered for marker in self._waf_markers) or any(
            marker in lowered for marker in ("滑块验证".encode("utf-8"), "人机验证".encode("utf-8"))
        )

    def _close(self) -> None:
        driver, self.driver = self.driver, None
        self.session_id = None
        self.keywords = None
        self.request = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    @staticmethod
    def _seed_browser_cookies(driver: Any, client: Any) -> None:
        for cookie in client.cookie_jar:
            domain = str(cookie.domain or ALLOWED_HOST).lstrip(".")
            if domain != ALLOWED_HOST and not domain.endswith(f".{ALLOWED_HOST}"):
                continue
            value: dict[str, Any] = {
                "name": str(cookie.name),
                "value": str(cookie.value),
                "path": str(cookie.path or "/"),
                "secure": bool(cookie.secure),
            }
            if cookie.expiry:
                value["expiry"] = int(cookie.expiry)
            # Selenium rejects a leading dot on some Edge versions; the
            # host-only domain is sufficient because the APIs share the host.
            try:
                driver.add_cookie(value)
            except Exception:
                continue

    @staticmethod
    def _sync_browser_cookies(driver: Any, client: Any) -> None:
        for raw in driver.get_cookies() or []:
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            domain = str(raw.get("domain") or ALLOWED_HOST).lstrip(".")
            if domain != ALLOWED_HOST and not domain.endswith(f".{ALLOWED_HOST}"):
                continue
            path = str(raw.get("path") or "/")
            expiry = raw.get("expiry")
            try:
                expiry = int(expiry) if expiry is not None else None
            except (TypeError, ValueError):
                expiry = None
            cookie = Cookie(
                version=0,
                name=name,
                value=str(raw.get("value") or ""),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=False,
                path=path,
                path_specified=True,
                secure=bool(raw.get("secure")),
                expires=expiry,
                discard=expiry is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": bool(raw.get("httpOnly"))},
                rfc2109=False,
            )
            client.cookie_jar.set_cookie(cookie)

    def _browser_request(self, driver: Any, session: Any, request: dict[str, Any]) -> dict[str, Any]:
        driver.set_script_timeout(30)
        result = driver.execute_async_script(
            """
            const payload = arguments[0];
            const visitorId = arguments[1];
            const done = arguments[arguments.length - 1];
            fetch('/shopApi/Order/list', {
              method: 'POST',
              credentials: 'include',
              headers: {
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'Visitorid': visitorId
              },
              body: JSON.stringify(payload)
            }).then(async response => done({
              status: response.status,
              content_type: response.headers.get('content-type') || '',
              text: await response.text()
            })).catch(error => done({error: String(error)}));
            """,
            self._payload(session, request),
            str(session.client.visitor_id),
        )
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError(str((result or {}).get("error") or "浏览器订单请求失败")[:200])
        text = str(result.get("text") or "")
        raw = text.encode("utf-8", "ignore")
        status = int(result.get("status") or 200)
        content_type = str(result.get("content_type") or "")
        if is_waf_response(raw, content_type=content_type, status=status):
            # Keep the challenge document intact; its inline slider script is
            # required for the Edge page to complete verification.
            raise WafChallengeRequired(text[:512_000])
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("浏览器会话仍未返回订单 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("浏览器会话返回的订单数据格式无效")
        return payload

    def _render_challenge(self, driver: Any, html_text: str) -> dict[str, Any]:
        try:
            driver.execute_script("document.open(); document.write(arguments[0]); document.close();", html_text)
        except Exception as exc:
            self._close()
            raise RuntimeError("无法在 Edge 中显示订单 WAF 验证页") from exc
        return {
            "status": "awaiting_verification",
            "detail": "请在已打开的 Edge 窗口完成阿里云滑块，然后点击“验证完成并继续查询”",
        }

    def _save_result(self, session: Any, request: dict[str, Any], result: dict[str, Any]) -> None:
        cached = {
            "orders": result["orders"],
            "pagination": result["pagination"],
        }
        with session.lock:
            session.remember_orders(cached["orders"])
            if self._sessions.cache_seconds:
                session.store_cache(
                    (request["status"], request["page"], request["page_size"]),
                    cached,
                    self._sessions.now() + self._sessions.cache_seconds,
                )

    def _success(self, session: Any, request: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        self._sync_browser_cookies(self.driver, session.client)
        self._save_result(session, request, result)
        response = {
            "status": "success",
            "session_id": session.session_id,
            "expires_in": self._sessions.remaining(session),
            "verification": {"status": "verified", "mode": "browser", "attempts": 0},
            **result,
        }
        self._close()
        return response

    def start(self, data: Any) -> dict[str, Any]:
        request = self._validated_request(data)
        with self.lock:
            self._close()
            try:
                session = self._sessions.get(request["session_id"], request["keywords"])
            except OrderQuerySessionExpired:
                self._close()
                raise
            if not session.ticket:
                raise OrderQuerySessionExpired()
            try:
                from selenium import webdriver
                from selenium.webdriver.edge.options import Options
            except ImportError as exc:
                raise RuntimeError("缺少浏览器验证组件，请执行 python -m pip install -r backend/requirements.txt") from exc
            options = Options()
            options.add_argument(f"--user-data-dir={self._profile_path}")
            options.add_argument("--start-maximized")
            options.add_argument("--no-first-run")
            options.add_argument("--disable-features=EdgeFirstRunExperience")
            try:
                driver = webdriver.Edge(options=options)
                self.driver = driver
                self.session_id = session.session_id
                self.keywords = request["keywords"]
                self.request = {key: request[key] for key in ("status", "page", "page_size")}
                driver.get(f"{BASE_URL}/order")
                self._seed_browser_cookies(driver, session.client)
                # Cookies must be added after the first navigation, then reload
                # so the browser request sees the verified urllib session.
                driver.get(f"{BASE_URL}/order")
                if self._is_waf_html(driver.page_source):
                    return self._render_challenge(driver, driver.page_source)
                payload = self._browser_request(driver, session, request)
                normalized = normalize_order_list(payload, page=request["page"], page_size=request["page_size"])
                return self._success(session, request, normalized)
            except WafChallengeRequired as exc:
                return self._render_challenge(driver, str(exc))
            except OrderQuerySessionExpired:
                self._close()
                raise
            except Exception as exc:
                self._close()
                raise RuntimeError(f"订单浏览器验证启动失败：{str(exc)[:160]}") from exc

    def complete(self, data: Any) -> dict[str, Any]:
        request = self._validated_request(data)
        with self.lock:
            if self.driver is None or self.session_id != request["session_id"] or self.keywords != request["keywords"]:
                raise RuntimeError("没有等待完成的订单浏览器验证会话")
            expected = self.request or {}
            if any(expected.get(key) != request[key] for key in ("status", "page", "page_size")):
                raise OrderQueryInputError("订单浏览器验证上下文已变化，请重新发起验证")
            session = self._sessions.get(request["session_id"], request["keywords"])
            if not session.ticket:
                self._close()
                raise OrderQuerySessionExpired()
            driver = self.driver
            try:
                # Reload the first-party page before replaying the API call.
                # Aliyun may leave the challenge DOM in place after the slider
                # sets its cookie; reloading lets the browser use that cookie.
                driver.get(f"{BASE_URL}/order")
                payload = self._browser_request(driver, session, request)
                normalized = normalize_order_list(payload, page=request["page"], page_size=request["page_size"])
                return self._success(session, request, normalized)
            except WafChallengeRequired as exc:
                return self._render_challenge(driver, str(exc))
            except OrderQuerySessionExpired:
                self._close()
                raise
            except Exception as exc:
                self._close()
                raise RuntimeError(f"订单浏览器验证同步失败：{str(exc)[:160]}") from exc


__all__ = ["OrderQueryBrowserVerificationManager"]
