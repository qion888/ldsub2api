"""Dedicated browser session for order-query WAF challenges.

The monitor and order lookup flows intentionally use separate managers and
profiles.  A monitor verification may be running while a buyer is solving an
order lookup challenge; sharing the driver would mix cookies and requests.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Keep imports explicit here instead of importing the service module (which
# would create a service/manager cycle during application startup).
from monitor_core.storefront import ALLOWED_HOST, ALLOWED_HOSTS, WAF_MARKERS, WafChallengeRequired, is_waf_response

from .client import BASE_URL, ORDER_LIST_PATH, normalize_order_list
from .errors import OrderQueryInputError, OrderQuerySessionExpired


class OrderQueryBrowserVerificationManager:
    """Open an isolated browser, let the user solve Aliyun WAF, then replay one order query."""

    WAF_LEASE_SECONDS = 900
    KEEPALIVE_SECONDS = 15
    READY_OBSERVATIONS = 2
    MAX_COMPLETION_REPLAYS = 2
    _WAF_COOKIE_MARKERS = (
        "acw_",
        "aliyun",
        "captcha",
        "cdn_sec",
        "server_session",
        "ssxmod_",
        "waf",
    )

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
        self.browser_process: Any = None
        self.browser_name: str | None = None
        self.browser_mode: str | None = None
        self._active_profile_path: Path | None = None
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        self.session_id: str | None = None
        self.keywords: str | None = None
        self.request: dict[str, int] | None = None
        self._challenge_dom_seen = False
        self._challenge_cookie_baseline = ""
        self._challenge_observation = ""
        self._challenge_observation_count = 0
        self._challenge_ready = False
        self._completion_replays = 0

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
        keepalive_stop, keepalive_thread = self._keepalive_stop, self._keepalive_thread
        self._keepalive_stop = threading.Event()
        self._keepalive_thread = None
        keepalive_stop.set()
        if keepalive_thread is not None and keepalive_thread is not threading.current_thread():
            keepalive_thread.join(timeout=2)
        driver, self.driver = self.driver, None
        browser_process, self.browser_process = self.browser_process, None
        active_profile, self._active_profile_path = self._active_profile_path, None
        self.browser_name = None
        self.browser_mode = None
        self.session_id = None
        self.keywords = None
        self.request = None
        self._challenge_dom_seen = False
        self._challenge_cookie_baseline = ""
        self._challenge_observation = ""
        self._challenge_observation_count = 0
        self._challenge_ready = False
        self._completion_replays = 0
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
            service_process = getattr(getattr(driver, "service", None), "process", None)
            self._terminate_process(service_process)
        self._terminate_process(browser_process)
        if active_profile is not None:
            shutil.rmtree(active_profile, ignore_errors=True)

    @staticmethod
    def _terminate_process(process: Any) -> None:
        if process is None:
            return
        try:
            if process.poll() is not None:
                return
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _start_keepalive(self, session: Any) -> None:
        self._keepalive_stop.set()
        stop = threading.Event()
        self._keepalive_stop = stop

        def keepalive() -> None:
            while not stop.wait(self.KEEPALIVE_SECONDS):
                try:
                    self._sessions.renew(session, ttl_seconds=self.WAF_LEASE_SECONDS)
                except Exception:
                    return

        self._keepalive_thread = threading.Thread(
            target=keepalive,
            name="order-query-waf-keepalive",
            daemon=True,
        )
        self._keepalive_thread.start()

    def _browser_order(self) -> list[str]:
        requested = os.environ.get("LDXP_WAF_BROWSER", "auto").strip().lower()
        if requested not in {"", "auto"}:
            aliases = {"google-chrome": "chrome", "msedge": "edge"}
            requested = aliases.get(requested, requested)
            if requested not in {"edge", "chrome", "chromium", "firefox"}:
                raise RuntimeError("LDXP_WAF_BROWSER must be auto, edge, chrome, chromium, or firefox")
            return [requested]
        if sys.platform.startswith("linux"):
            return ["chrome", "chromium", "edge", "firefox"]
        if sys.platform == "darwin":
            return ["chrome", "edge", "firefox"]
        return ["edge", "chrome", "chromium", "firefox"]

    def _create_driver(self) -> tuple[Any, str]:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions
            from selenium.webdriver.edge.options import Options as EdgeOptions
            from selenium.webdriver.firefox.options import Options as FirefoxOptions
        except ImportError as exc:
            raise RuntimeError(
                "缺少浏览器验证组件，请执行 python -m pip install -r backend/requirements.txt"
            ) from exc

        self._profile_path.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        common_chromium = (
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-dev-shm-usage",
            "--lang=zh-CN",
        )
        for browser in self._browser_order():
            profile_path = Path(tempfile.mkdtemp(prefix="order-waf-", dir=str(self._profile_path)))
            driver: Any = None
            try:
                if browser == "edge":
                    options = EdgeOptions()
                    options.add_argument(f"--user-data-dir={profile_path}")
                    options.add_argument("--start-maximized")
                    for argument in common_chromium:
                        options.add_argument(argument)
                    driver = webdriver.Edge(options=options)
                elif browser in {"chrome", "chromium"}:
                    options = ChromeOptions()
                    options.add_argument(f"--user-data-dir={profile_path}")
                    options.add_argument("--start-maximized")
                    for argument in common_chromium:
                        options.add_argument(argument)
                    if browser == "chromium":
                        for binary in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
                            if Path(binary).exists():
                                options.binary_location = binary
                                break
                    driver = webdriver.Chrome(options=options)
                else:
                    options = FirefoxOptions()
                    options.add_argument("-profile")
                    options.add_argument(str(profile_path))
                    options.set_preference("intl.accept_languages", "zh-CN,zh")
                    driver = webdriver.Firefox(options=options)
                self._active_profile_path = profile_path
                return driver, browser
            except Exception as exc:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                shutil.rmtree(profile_path, ignore_errors=True)
                errors.append(f"{browser}: {str(exc)[:120]}")
        detail = "; ".join(errors) or "no browser candidates"
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            detail += "; Linux needs a graphical session (DISPLAY/WAYLAND_DISPLAY) for the interactive challenge"
        raise RuntimeError(f"无法启动 WAF 验证浏览器（{detail}）")

    @staticmethod
    def _is_storefront_cookie_domain(value: Any) -> bool:
        domain = str(value or "").strip().lstrip(".").lower()
        if domain in ALLOWED_HOSTS:
            return True
        # The payment host also legitimately receives cookies scoped to the
        # parent ldxp.cn domain. Do not broaden this to arbitrary suffixes.
        return any(
            allowed.count(".") >= 2 and domain == allowed.split(".", 1)[1]
            for allowed in ALLOWED_HOSTS
        )

    @classmethod
    def _seed_browser_cookies(cls, driver: Any, client: Any) -> None:
        for cookie in client.cookie_jar:
            domain = str(cookie.domain or ALLOWED_HOST).lstrip(".")
            if not cls._is_storefront_cookie_domain(domain):
                continue
            value: dict[str, Any] = {
                "name": str(cookie.name),
                "value": str(cookie.value),
                "path": str(cookie.path or "/"),
                "secure": bool(cookie.secure),
            }
            if cookie.expires:
                value["expiry"] = int(cookie.expires)
            # Selenium rejects a leading dot on some Edge versions; the
            # host-only domain is sufficient because the APIs share the host.
            try:
                driver.add_cookie(value)
            except Exception:
                continue

    @classmethod
    def _sync_browser_cookies(cls, driver: Any, client: Any) -> None:
        for raw in driver.get_cookies() or []:
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            domain = str(raw.get("domain") or ALLOWED_HOST).lstrip(".")
            if not cls._is_storefront_cookie_domain(domain):
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
            raise WafChallengeRequired(text[:512_000])
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("浏览器会话仍未返回订单 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("浏览器会话返回的订单数据格式无效")
        return payload

    def _browser_probe_captcha(self, driver: Any, session: Any) -> dict[str, Any]:
        """Probe the captcha endpoint in the interactive browser context.

        The initial WAF response can happen before the OCR flow receives a
        ticket. A browser request at this point is enough to load Aliyun's
        challenge and, after completion, leave its verification cookie in the
        same context that the resumed urllib session will use.
        """
        driver.set_script_timeout(30)
        result = driver.execute_async_script(
            """
            const done = arguments[arguments.length - 1];
            fetch('/shopApi/Common/captchaStart', {
              method: 'POST',
              credentials: 'include',
              headers: {
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json'
              },
              body: JSON.stringify({code: ''})
            }).then(async response => done({
              status: response.status,
              content_type: response.headers.get('content-type') || '',
              text: await response.text()
            })).catch(error => done({error: String(error)}));
            """
        )
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError(str((result or {}).get("error") or "浏览器验证码请求失败")[:200])
        text = str(result.get("text") or "")
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = None
        content_type = str(result.get("content_type") or "")
        if is_waf_response(text.encode("utf-8", "ignore"), content_type=content_type, status=status):
            raise WafChallengeRequired(text[:512_000])
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("浏览器验证码接口返回了无法解析的数据") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("浏览器验证码接口返回的数据格式无效")
        return payload

    @classmethod
    def _cookie_fingerprint(cls, cookies: Any) -> str:
        selected: list[tuple[str, str, str, str]] = []
        for raw in cookies if isinstance(cookies, list) else []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not any(marker in name.lower() for marker in cls._WAF_COOKIE_MARKERS):
                continue
            selected.append((
                name,
                str(raw.get("domain") or ""),
                str(raw.get("path") or "/"),
                str(raw.get("value") or ""),
            ))
        if not selected:
            return ""
        encoded = json.dumps(sorted(selected), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _browser_snapshot(driver: Any) -> tuple[str, str, list[dict[str, Any]]]:
        handles = getattr(driver, "window_handles", None)
        if handles is not None and not list(handles):
            raise RuntimeError("browser window is closed")
        current_url = str(getattr(driver, "current_url", "") or "")
        page_source = str(getattr(driver, "page_source", "") or "")
        getter = getattr(driver, "get_cookies", None)
        cookies = getter() if callable(getter) else []
        return current_url, page_source, cookies if isinstance(cookies, list) else []

    def _challenge_is_visible(self, driver: Any, page_source: str) -> bool:
        script = """
        const selectors = [
          '#aliyunCaptcha', '[id*="aliyunCaptcha"]', '[class*="aliyunCaptcha"]',
          '#nc_1_wrapper', '.nc_wrapper', '.nc-container', '[class*="sliding-slider"]'
        ];
        return selectors.some(selector => Array.from(document.querySelectorAll(selector)).some(element => {
          const style = window.getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0
            && rect.width > 1 && rect.height > 1;
        }));
        """
        try:
            visible = driver.execute_script(script)
        except Exception:
            visible = None
        return bool(visible) if isinstance(visible, bool) else self._is_waf_html(page_source)

    def _reset_challenge_observation(self, cookies: Any) -> None:
        self._challenge_dom_seen = True
        self._challenge_cookie_baseline = self._cookie_fingerprint(cookies)
        self._challenge_observation = ""
        self._challenge_observation_count = 0
        self._challenge_ready = False

    def _observe_challenge(self, driver: Any) -> bool:
        _current_url, page_source, cookies = self._browser_snapshot(driver)
        challenge_visible = self._challenge_is_visible(driver, page_source)
        has_challenge_markup = self._is_waf_html(page_source)
        if challenge_visible or (has_challenge_markup and not self._challenge_dom_seen):
            self._reset_challenge_observation(cookies)
            return False
        if not self._challenge_dom_seen:
            return False

        cookie_fingerprint = self._cookie_fingerprint(cookies)
        observation = (
            f"verified_cookie:{cookie_fingerprint}"
            if cookie_fingerprint and cookie_fingerprint != self._challenge_cookie_baseline and not challenge_visible
            else ""
        )
        if observation and observation == self._challenge_observation:
            self._challenge_observation_count += 1
        elif observation:
            self._challenge_observation = observation
            self._challenge_observation_count = 1
        else:
            self._challenge_observation = ""
            self._challenge_observation_count = 0
        self._challenge_ready = self._challenge_observation_count >= self.READY_OBSERVATIONS
        return self._challenge_ready

    def _render_challenge(self, driver: Any, html_text: str, session: Any, request: dict[str, Any]) -> dict[str, Any]:
        del html_text
        if driver is None:
            raise RuntimeError("订单 WAF 验证浏览器不可用")
        try:
            # Keep the challenge request identical to the protected API. A
            # form POST changes the content type and can bind the slider to a
            # different request fingerprint.
            driver.execute_script(
                """
                const endpoint = arguments[0];
                const payload = arguments[1];
                fetch(endpoint, {
                  method: 'POST',
                  credentials: 'include',
                  headers: {
                    'Accept': 'application/json, text/plain, */*',
                    'Content-Type': 'application/json'
                  },
                  body: JSON.stringify(payload)
                }).then(async response => {
                  const text = await response.text();
                  const contentType = response.headers.get('content-type') || '';
                  if (contentType.toLowerCase().includes('text/html') || /aliyun|captcha|滑块|人机验证/i.test(text)) {
                    document.open('text/html', 'replace');
                    document.write(text);
                    document.close();
                    try { history.replaceState({}, '', endpoint); } catch (_) {}
                    return;
                  }
                  document.open('text/html', 'replace');
                  document.write('<pre id="order-waf-response"></pre>');
                  document.close();
                  const output = document.getElementById('order-waf-response');
                  if (output) output.textContent = text;
                }).catch(error => {
                  document.open('text/html', 'replace');
                  document.write('<pre id="order-waf-error"></pre>');
                  document.close();
                  const output = document.getElementById('order-waf-error');
                  if (output) output.textContent = String(error);
                });
                """,
                f"{BASE_URL}{ORDER_LIST_PATH}",
                self._payload(session, request),
            )
            _current_url, page_source, cookies = self._browser_snapshot(driver)
            self._reset_challenge_observation(cookies)
            # A completed document is normally available by this point. Keep
            # the post-navigation cookies as the baseline if the challenge is
            # already visible, so the initial WAF cookie is not mistaken for
            # proof that a person completed the slider.
            if self._challenge_is_visible(driver, page_source) or self._is_waf_html(page_source):
                self._reset_challenge_observation(cookies)
        except Exception as exc:
            self._close()
            raise RuntimeError("无法在独立浏览器中显示订单 WAF 验证页") from exc
        return {
            "status": "awaiting_verification",
            "detail": "请在已打开的独立浏览器窗口完成阿里云滑块，完成后页面会自动继续查询",
            "browser": self.browser_name,
            "session_id": session.session_id,
            "expires_in": self._sessions.renew(session, ttl_seconds=self.WAF_LEASE_SECONDS),
            "waf_request": {
                "keywords": request["keywords"],
                "status": request["status"],
                "page": request["page"],
                "page_size": request["page_size"],
            },
        }

    def _resume_search(self, session: Any, request: dict[str, Any]) -> dict[str, Any]:
        """Return control to the service when WAF happened before ticket creation."""
        self._sync_browser_cookies(self.driver, session.client)
        response = {
            "status": "verified",
            "resume_search": True,
            "session_id": session.session_id,
            "expires_in": self._sessions.remaining(session),
            "verification": {"status": "verified", "mode": "browser", "attempts": 0},
            "waf_request": {
                "keywords": request["keywords"],
                "status": request["status"],
                "page": request["page"],
                "page_size": request["page_size"],
            },
        }
        self._close()
        return response

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
            self._sessions.renew(session, ttl_seconds=self.WAF_LEASE_SECONDS)
            driver: Any = None
            try:
                driver, self.browser_name = self._create_driver()
                self.driver = driver
                self.session_id = session.session_id
                self.keywords = request["keywords"]
                self.request = {key: request[key] for key in ("status", "page", "page_size")}
                self._start_keepalive(session)
                driver.get(f"{BASE_URL}/order")
                self._seed_browser_cookies(driver, session.client)
                # Cookies must be added after the first navigation, then reload
                # so the browser request sees the verified urllib session.
                driver.get(f"{BASE_URL}/order")
                if self._is_waf_html(driver.page_source):
                    return self._render_challenge(driver, driver.page_source, session, request)
                if not session.ticket:
                    try:
                        self._browser_probe_captcha(driver, session)
                    except WafChallengeRequired as exc:
                        return self._render_challenge(driver, str(exc), session, request)
                    try:
                        # Captcha creation and order listing can be protected
                        # by different edge rules. Probe the actual protected
                        # order endpoint too, so a successful captcha probe
                        # does not falsely tell the UI that the query can
                        # resume while the list API is still challenged.
                        self._browser_request(driver, session, request)
                    except WafChallengeRequired as exc:
                        return self._render_challenge(driver, str(exc), session, request)
                    except Exception:
                        # A missing ticket is expected to produce a business
                        # response; the resumed service flow will create the
                        # ticket and perform the real query.
                        pass
                    return self._resume_search(session, request)
                payload = self._browser_request(driver, session, request)
                normalized = normalize_order_list(payload, page=request["page"], page_size=request["page_size"])
                return self._success(session, request, normalized)
            except WafChallengeRequired as exc:
                return self._render_challenge(driver, str(exc), session, request)
            except OrderQuerySessionExpired:
                self._close()
                raise
            except Exception as exc:
                self._close()
                raise RuntimeError(f"订单浏览器验证启动失败：{str(exc)[:160]}") from exc

    def status(self, data: Any) -> dict[str, Any]:
        """Inspect the challenge page without replaying the protected API."""
        request = self._validated_request(data)
        with self.lock:
            if self.driver is None or self.session_id != request["session_id"] or self.keywords != request["keywords"]:
                raise RuntimeError("没有等待完成的订单浏览器验证会话")
            try:
                session = self._sessions.get(request["session_id"], request["keywords"])
                self._sessions.renew(session, ttl_seconds=self.WAF_LEASE_SECONDS)
            except OrderQuerySessionExpired:
                self._close()
                raise
            driver = self.driver
            try:
                ready = self._observe_challenge(driver)
                return {
                    "status": "ready" if ready else "awaiting_verification",
                    "ready": ready,
                    "browser": self.browser_name,
                    "expires_in": self._sessions.remaining(session),
                }
            except OrderQuerySessionExpired:
                self._close()
                raise
            except Exception as exc:
                self._close()
                raise RuntimeError(f"订单浏览器验证窗口已关闭：{str(exc)[:160]}") from exc

    def complete(self, data: Any) -> dict[str, Any]:
        request = self._validated_request(data)
        with self.lock:
            if self.driver is None or self.session_id != request["session_id"] or self.keywords != request["keywords"]:
                raise RuntimeError("没有等待完成的订单浏览器验证会话")
            expected = self.request or {}
            if any(expected.get(key) != request[key] for key in ("status", "page", "page_size")):
                raise OrderQueryInputError("订单浏览器验证上下文已变化，请重新发起验证")
            session = self._sessions.get(request["session_id"], request["keywords"])
            try:
                self._sessions.renew(session, ttl_seconds=self.WAF_LEASE_SECONDS)
            except OrderQuerySessionExpired:
                self._close()
                raise
            driver = self.driver
            try:
                if not session.ticket:
                    if not self._challenge_ready:
                        return {
                            "status": "awaiting_verification",
                            "ready": False,
                            "browser": self.browser_name,
                            "expires_in": self._sessions.remaining(session),
                        }
                    return self._resume_search(session, request)
                if not self._challenge_ready:
                    return {
                        "status": "awaiting_verification",
                        "ready": False,
                        "browser": self.browser_name,
                        "expires_in": self._sessions.remaining(session),
                    }
                if self._completion_replays >= self.MAX_COMPLETION_REPLAYS:
                    self._close()
                    raise RuntimeError("订单 WAF 验证重放次数已用尽，请重新发起查询")
                self._completion_replays += 1
                # Reload the first-party page before replaying the API call.
                # Aliyun may leave the challenge DOM in place after the slider
                # sets its cookie; reloading lets the browser use that cookie.
                driver.get(f"{BASE_URL}/order")
                payload = self._browser_request(driver, session, request)
                normalized = normalize_order_list(payload, page=request["page"], page_size=request["page_size"])
                return self._success(session, request, normalized)
            except WafChallengeRequired as exc:
                return self._render_challenge(driver, str(exc), session, request)
            except OrderQuerySessionExpired:
                self._close()
                raise
            except Exception as exc:
                self._close()
                raise RuntimeError(f"订单浏览器验证同步失败：{str(exc)[:160]}") from exc


__all__ = ["OrderQueryBrowserVerificationManager"]
