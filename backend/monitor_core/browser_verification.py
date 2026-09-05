"""Interactive browser sessions used to complete storefront WAF verification."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .storefront import is_waf_response, waf_proxy_config
from .windows_input import DISPATCHED, FAILED, NOT_READY, attempt_native_slider


class BrowserVerificationManager:
    """Coordinate one persistent browser session across one or many shops.

    Chromium starts as a normal persistent browser before WebDriver attaches, so
    VPN/proxy state and the first challenge navigation retain a browser context.
    Status polling is passive; only an explicit completion replays goodsList.
    """

    CDP_START_TIMEOUT = 15.0
    READY_OBSERVATIONS = 2
    MAX_COMPLETION_REPLAYS = 2
    _CHALLENGE_ID = re.compile(r"[A-Za-z0-9._-]{8,128}")
    _VISITOR_ID = re.compile(r"[A-Za-z0-9_-]{6,80}")
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
        database: Callable[[], Any],
        worker_lock: Any,
        record_shop_fetch: Callable[..., dict[str, Any]],
        goods_list_rows: Callable[[dict[str, Any]], tuple[list[dict[str, Any]], dict[str, Any]]],
        normalize_goods: Callable[[dict[str, Any], str], dict[str, Any]],
        first_value: Callable[[dict[str, Any], tuple[str, ...]], Any],
        waf_error: type[Exception],
        waf_markers: tuple[bytes, ...],
        profile_path: Path,
        request_waiter: Callable[[], None] | None = None,
        publish_browser_session: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._database = database
        self._worker_lock = worker_lock
        self._record_shop_fetch = record_shop_fetch
        self._goods_list_rows = goods_list_rows
        self._normalize_goods = normalize_goods
        self._first_value = first_value
        self._waf_error = waf_error
        self._waf_markers = waf_markers
        self._profile_path = profile_path
        self._request_waiter = request_waiter or (lambda: None)
        self._publish_browser_session_callback = publish_browser_session
        self.lock = threading.RLock()
        self.driver: Any = None
        self.browser_process: Any = None
        self.shop_id: int | None = None
        self.browser_name: str | None = None
        self.browser_mode: str | None = None
        self.proxy_mode = "system"
        self.batch_queue: list[int] = []
        self.batch_shop_ids: tuple[int, ...] = ()
        self.batch_results: list[dict[str, Any]] = []
        self.batch_total = 0
        self.batch_completed = 0
        self.batch_current_shop_id: int | None = None
        self.batch_active = False
        self.challenge_id: str | None = None
        self.challenge_attempts = 0
        self._completion_replays = 0
        self._challenge_dom_seen = False
        self._challenge_cookie_baseline = ""
        self._challenge_observation = ""
        self._challenge_observation_count = 0
        self._challenge_ready = False
        self._terminal_status: str | None = None
        self._terminal_detail = ""
        self._last_completed_challenge_id: str | None = None
        self._last_completed_result: dict[str, Any] | None = None
        # Automatic input is deliberately bounded per shop.  A failed native
        # drag must never turn status polling into a retry loop or invalidate a
        # browser session that the user can still complete manually.
        self.automatic_attempted_shop_ids: set[int] = set()
        self.automatic_status = "not_started"
        self._automatic_probe_count = 0
        self._automatic_probe_shop_id: int | None = None

    @staticmethod
    def _request_data(shop: Any, current: int) -> dict[str, Any]:
        return {
            "token": shop["token"],
            "keywords": shop["keywords"] or "",
            "category_id": shop["category_id"] or "",
            "goods_type": shop["goods_type"] or "card",
            "current": current,
            "pageSize": 50,
        }

    def _is_waf_html(self, text: str) -> bool:
        encoded = text.encode("utf-8", "ignore").lower()
        return any(marker.lower() in encoded for marker in self._waf_markers)

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

    def _close(self) -> None:
        driver, self.driver = self.driver, None
        process, self.browser_process = self.browser_process, None
        self.shop_id = None
        self.browser_name = None
        service_process = getattr(getattr(driver, "service", None), "process", None) if driver is not None else None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        self._terminate_process(service_process)
        self._terminate_process(process)

    def _reset_batch(self) -> None:
        self.batch_queue = []
        self.batch_shop_ids = ()
        self.batch_results = []
        self.batch_total = 0
        self.batch_completed = 0
        self.batch_current_shop_id = None
        self.batch_active = False
        self.browser_mode = None
        self.proxy_mode = "system"
        self.challenge_id = None
        self.challenge_attempts = 0
        self._completion_replays = 0
        self._challenge_dom_seen = False
        self._challenge_cookie_baseline = ""
        self._challenge_observation = ""
        self._challenge_observation_count = 0
        self._challenge_ready = False
        self._terminal_status = None
        self._terminal_detail = ""
        self.automatic_attempted_shop_ids = set()
        self.automatic_status = "not_started"
        self._automatic_probe_count = 0
        self._automatic_probe_shop_id = None

    @staticmethod
    def _proxy_config() -> dict[str, Any]:
        return waf_proxy_config()

    @staticmethod
    def _apply_firefox_proxy(options: Any, proxy: dict[str, Any]) -> None:
        mode = proxy["mode"]
        if mode == "system":
            options.set_preference("network.proxy.type", 5)
            return
        if mode == "direct":
            options.set_preference("network.proxy.type", 0)
            return
        options.set_preference("network.proxy.type", 1)
        options.set_preference("network.proxy.http", proxy["host"])
        options.set_preference("network.proxy.http_port", proxy["port"])
        options.set_preference("network.proxy.ssl", proxy["host"])
        options.set_preference("network.proxy.ssl_port", proxy["port"])
        options.set_preference("network.proxy.share_proxy_settings", True)

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def _browser_executable(browser: str) -> Path | None:
        env_name = "LDXP_WAF_EDGE_BINARY" if browser == "edge" else "LDXP_WAF_CHROME_BINARY"
        configured = os.environ.get(env_name, "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            return candidate if candidate.is_file() else None

        names = {
            "edge": ("msedge", "microsoft-edge", "microsoft-edge-stable"),
            "chrome": ("chrome", "google-chrome", "google-chrome-stable"),
            "chromium": ("chromium", "chromium-browser"),
        }[browser]
        for name in names:
            resolved = shutil.which(name)
            if resolved:
                return Path(resolved)

        candidates: list[Path] = []
        if sys.platform == "win32":
            roots = [
                os.environ.get("PROGRAMFILES(X86)"),
                os.environ.get("PROGRAMFILES"),
                os.environ.get("LOCALAPPDATA"),
            ]
            suffixes = (
                ("Microsoft", "Edge", "Application", "msedge.exe"),
            ) if browser == "edge" else (
                ("Google", "Chrome", "Application", "chrome.exe"),
                ("Chromium", "Application", "chrome.exe"),
            )
            candidates.extend(
                Path(root).joinpath(*suffix)
                for root in roots if root
                for suffix in suffixes
            )
        elif sys.platform == "darwin":
            candidates.extend({
                "edge": [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")],
                "chrome": [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")],
                "chromium": [Path("/Applications/Chromium.app/Contents/MacOS/Chromium")],
            }[browser])
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    def _chromium_command(
        self,
        browser: str,
        executable: Path,
        port: int,
        initial_url: str,
        proxy: dict[str, Any],
    ) -> list[str]:
        command = [
            str(executable),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self._profile_path}",
            "--profile-directory=Default",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
        ]
        if browser == "edge":
            command.append("--disable-features=EdgeFirstRunExperience")
        if proxy["mode"] == "direct":
            command.append("--no-proxy-server")
        elif proxy["server"]:
            command.append(f"--proxy-server={proxy['server']}")
        command.append(initial_url)
        return command

    @staticmethod
    def _read_cdp_json(port: int, path: str) -> Any:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.35)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8", "replace"))
            if response.status != 200:
                raise RuntimeError(f"browser debugger returned HTTP {response.status}")
            return payload
        finally:
            connection.close()

    @classmethod
    def _cdp_has_initial_target(cls, targets: Any, initial_url: str) -> bool:
        expected = urlparse(initial_url)
        for target in targets if isinstance(targets, list) else []:
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            actual_value = str(target.get("url") or "")
            actual = urlparse(actual_value)
            if expected.scheme in {"http", "https"}:
                if (
                    actual.scheme.lower() == expected.scheme.lower()
                    and (actual.hostname or "").lower() == (expected.hostname or "").lower()
                    and actual.port == expected.port
                ):
                    return True
            elif actual_value == initial_url:
                return True
        return False

    @classmethod
    def _wait_for_cdp(cls, port: int, process: Any, initial_url: str) -> None:
        deadline = time.monotonic() + cls.CDP_START_TIMEOUT
        last_error = "browser did not expose its local debugger"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("browser exited before its local debugger was ready")
            try:
                version = cls._read_cdp_json(port, "/json/version")
                targets = cls._read_cdp_json(port, "/json/list")
                if isinstance(version, dict) and version.get("webSocketDebuggerUrl") and cls._cdp_has_initial_target(
                    targets, initial_url
                ):
                    return
                last_error = "browser debugger is ready but the initial page target is still loading"
            except Exception as exc:
                last_error = str(exc)[:120]
            time.sleep(0.1)
        raise RuntimeError(f"browser local debugger did not become ready: {last_error}")

    @staticmethod
    def _prepare_attached_chromium(driver: Any) -> None:
        source = """
        for (const key of Object.getOwnPropertyNames(window)) {
          if (/^cdc_[A-Za-z0-9]+_(Array|Object|Promise|Proxy|Symbol|JSON|Window)$/.test(key)) {
            try { delete window[key]; } catch (_) {}
          }
        }
        try {
          Object.defineProperty(Navigator.prototype, 'webdriver', {
            get: () => undefined,
            configurable: true,
          });
        } catch (_) {}
        """
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        driver.execute_script(source)

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

    def _create_driver(self, initial_url: str) -> tuple[Any, str]:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions
            from selenium.webdriver.edge.options import Options as EdgeOptions
            from selenium.webdriver.firefox.options import Options as FirefoxOptions
        except ImportError as exc:
            raise RuntimeError(
                "Selenium is required for WAF verification; run python -m pip install -r backend/requirements.txt"
            ) from exc

        self._profile_path.mkdir(parents=True, exist_ok=True)
        proxy = self._proxy_config()
        self.proxy_mode = str(proxy["mode"])
        errors: list[str] = []
        for browser in self._browser_order():
            driver: Any = None
            process: Any = None
            try:
                if browser in {"edge", "chrome", "chromium"}:
                    executable = self._browser_executable(browser)
                    if executable is None:
                        raise RuntimeError("browser executable was not found")
                    port = self._available_port()
                    popen_kwargs: dict[str, Any] = {
                        "stdin": subprocess.DEVNULL,
                        "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL,
                    }
                    if sys.platform == "win32":
                        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    else:
                        popen_kwargs["start_new_session"] = True
                    process = subprocess.Popen(
                        self._chromium_command(browser, executable, port, initial_url, proxy),
                        **popen_kwargs,
                    )
                    self._wait_for_cdp(port, process, initial_url)
                    options = EdgeOptions() if browser == "edge" else ChromeOptions()
                    options.debugger_address = f"127.0.0.1:{port}"
                    driver = webdriver.Edge(options=options) if browser == "edge" else webdriver.Chrome(options=options)
                    # The browser was created without WebDriver's
                    # --enable-automation switch. Attaching only after its first
                    # real top-level navigation preserves that browser context.
                    self._prepare_attached_chromium(driver)
                    self.browser_process = process
                    self.browser_mode = "native_cdp"
                else:
                    options = FirefoxOptions()
                    options.add_argument("-profile")
                    options.add_argument(str(self._profile_path))
                    self._apply_firefox_proxy(options, proxy)
                    driver = webdriver.Firefox(options=options)
                    self.browser_mode = "webdriver"
                return driver, browser
            except Exception as exc:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                self._terminate_process(process)
                if self.browser_process is process:
                    self.browser_process = None
                errors.append(f"{browser}: {str(exc)[:120]}")
        detail = "; ".join(errors) or "no browser candidates"
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            detail += "; Linux needs a graphical session (DISPLAY/WAYLAND_DISPLAY) for the interactive challenge"
        raise RuntimeError(f"Unable to start a WAF browser ({detail})")

    @staticmethod
    def _same_origin(actual: str, expected: str) -> bool:
        try:
            actual_url = urlparse(str(actual or ""))
            expected_url = urlparse(str(expected or ""))
        except ValueError:
            return False
        return (
            actual_url.scheme.lower(),
            (actual_url.hostname or "").lower(),
            actual_url.port,
        ) == (
            expected_url.scheme.lower(),
            (expected_url.hostname or "").lower(),
            expected_url.port,
        )

    @staticmethod
    def _same_top_level_url(actual: str, expected: str) -> bool:
        try:
            actual_url = urlparse(str(actual or ""))
            expected_url = urlparse(str(expected or ""))
        except ValueError:
            return False
        return (
            actual_url.scheme.lower(),
            (actual_url.hostname or "").lower(),
            actual_url.port,
            actual_url.path.rstrip("/"),
        ) == (
            expected_url.scheme.lower(),
            (expected_url.hostname or "").lower(),
            expected_url.port,
            expected_url.path.rstrip("/"),
        )

    @classmethod
    def _cookie_fingerprint(cls, cookies: Any) -> str:
        selected: list[tuple[str, str, str, str]] = []
        for raw in cookies if isinstance(cookies, list) else []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "")
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

    def _publish_browser_state(self, driver: Any, shop: Any) -> None:
        callback = self._publish_browser_session_callback
        if callback is None:
            return
        try:
            user_agent = str(driver.execute_script("return navigator.userAgent") or "")[:512]
            visitor_id = str(driver.execute_script(
                "try { return window.localStorage.getItem('visitorId') || ''; } catch (_) { return ''; }"
            ) or "")
            if not self._VISITOR_ID.fullmatch(visitor_id):
                visitor_id = ""
            expected_host = (urlparse(str(shop["url"])).hostname or "").lower()
            cookies = []
            for raw in driver.get_cookies() or []:
                if not isinstance(raw, dict):
                    continue
                domain = str(raw.get("domain") or expected_host).lstrip(".").lower()
                if domain != expected_host and not expected_host.endswith(f".{domain}"):
                    continue
                cookies.append({
                    key: raw[key]
                    for key in ("name", "value", "domain", "path", "secure", "httpOnly", "expiry", "sameSite")
                    if key in raw
                })
            callback({
                "shop_id": int(shop["id"]),
                "url": str(shop["url"]),
                "browser": self.browser_name,
                "proxy_mode": self.proxy_mode,
                "user_agent": user_agent,
                "visitor_id": visitor_id,
                "cookies": cookies,
            })
        except Exception:
            # Publishing is an integration hook; a consumer failure must not
            # discard a catalog that was already fetched successfully.
            return

    def _browser_request(self, driver: Any, data: dict[str, Any]) -> dict[str, Any]:
        self._request_waiter()
        driver.set_script_timeout(30)
        result = driver.execute_async_script(
            """
            const payload = arguments[0];
            const done = arguments[arguments.length - 1];
            let visitorId = '';
            try {
              visitorId = window.localStorage.getItem('visitorId') || '';
              if (!visitorId) {
                visitorId = Math.random().toString(36).slice(2, 11);
                window.localStorage.setItem('visitorId', visitorId);
              }
            } catch (_) {}
            fetch('/shopApi/Shop/goodsList', {
              method: 'POST',
              credentials: 'include',
              headers: {
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                ...(visitorId ? {'Visitorid': visitorId} : {}),
              },
              body: JSON.stringify(payload)
            }).then(async response => done({
              status: response.status,
              content_type: response.headers.get('content-type') || '',
              text: await response.text()
            })).catch(error => done({error: String(error)}));
            """,
            data,
        )
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError(str((result or {}).get("error") or "browser request failed")[:200])
        text = str(result.get("text") or "")
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = None
        content_type = str(result.get("content_type") or "")
        if self._is_waf_html(text) or is_waf_response(
            text.encode("utf-8", "ignore"), content_type=content_type, status=status
        ):
            raise self._waf_error(text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("browser session returned non-JSON catalog data") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("browser session returned an invalid catalog payload")
        return payload

    def _attempt_automatic_slider(self, driver: Any) -> str:
        """Try one native Windows drag while preserving a manual fallback.

        The challenge DOM can take a few status polls to finish loading.  Probe
        at most three times while it is still not ready, then stop.  Once a drag
        is dispatched or fails, remember the shop so a failed slider cannot be
        replayed by the frontend's polling timer.
        """
        enabled = os.environ.get("LDXP_WAF_AUTO_VERIFY", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        shop_id = self.batch_current_shop_id
        if not enabled:
            return "disabled"
        if shop_id is None:
            return "not_retrying"
        if self._automatic_probe_shop_id != shop_id:
            self._automatic_probe_shop_id = shop_id
            self._automatic_probe_count = 0
            self.automatic_status = "not_started"
        if shop_id in self.automatic_attempted_shop_ids:
            return "not_retrying"
        if self._automatic_probe_count >= 3:
            return NOT_READY
        self._automatic_probe_count += 1
        try:
            outcome = attempt_native_slider(driver, self.browser_process)
        except Exception:
            outcome = FAILED
        if outcome in {DISPATCHED, FAILED}:
            self.automatic_attempted_shop_ids.add(shop_id)
        return outcome

    def _catalog(self, driver: Any, shop: Any, first_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        products: dict[str, dict[str, Any]] = {}
        for current in range(1, 51):
            payload = first_payload if current == 1 and first_payload is not None else self._browser_request(
                driver, self._request_data(shop, current)
            )
            rows, pagination = self._goods_list_rows(payload)
            for row in rows:
                product = self._normalize_goods(row, shop["token"])
                products[product["goods_key"]] = product
            total_value = self._first_value(pagination, ("total", "count", "total_count"))
            page_value = self._first_value(pagination, ("last_page", "lastPage", "pages", "page_count"))
            try:
                total = int(total_value) if total_value not in (None, "") else None
            except (TypeError, ValueError):
                total = None
            try:
                pages = int(page_value) if page_value not in (None, "") else None
            except (TypeError, ValueError):
                pages = None
            if not rows or len(rows) < 50 or (total is not None and current * 50 >= total) or (pages is not None and current >= pages):
                break
        return list(products.values())

    def _render_challenge(self, driver: Any, html_text: str) -> dict[str, Any]:
        del html_text
        if self.batch_current_shop_id is None:
            raise RuntimeError("WAF challenge has no current shop")
        shop = self._shop(self.batch_current_shop_id)
        try:
            current_url, page_source, cookies = self._browser_snapshot(driver)
            # The XHR body is intentionally never injected and its POST URL is
            # never opened as a GET. Preserve a real same-origin challenge
            # redirect; otherwise reload only the canonical first-party shop.
            parsed_current = urlparse(current_url)
            protected_api_page = parsed_current.path.rstrip("/") == "/shopApi/Shop/goodsList"
            keep_existing_challenge = (
                self._same_origin(current_url, shop["url"])
                and self._is_waf_html(page_source)
                and not protected_api_page
            )
            if not keep_existing_challenge:
                self._request_waiter()
                driver.get(shop["url"])
                _current_url, page_source, cookies = self._browser_snapshot(driver)
            # A background request can be challenged while the storefront page
            # itself remains accessible. Probe the protected endpoint from the
            # verified browser before exposing an unnecessary manual step.
            if not self._challenge_is_visible(driver, page_source):
                try:
                    probe_payload = self._browser_request(driver, self._request_data(shop, 1))
                except self._waf_error:
                    probe_payload = None
                except Exception as exc:
                    shop_id = self.batch_current_shop_id
                    if self.batch_queue and self.batch_queue[0] == shop_id:
                        self.batch_queue.pop(0)
                    self.batch_results.append({"id": shop_id, "ok": False, "error": str(exc)[:240]})
                    self.batch_completed += 1
                    return self._advance_batch_locked()
                else:
                    shop_id = self.batch_current_shop_id
                    if self.batch_queue and self.batch_queue[0] == shop_id:
                        self.batch_queue.pop(0)
                    try:
                        summary = self._sync_shop(driver, shop_id, first_payload=probe_payload)
                    except self._waf_error as exc:
                        self.batch_queue.insert(0, shop_id)
                    except Exception as exc:
                        self.batch_results.append({"id": shop_id, "ok": False, "error": str(exc)[:240]})
                        self.batch_completed += 1
                        return self._advance_batch_locked()
                    else:
                        self.batch_results.append({"id": shop_id, "ok": True, "data": summary})
                        self.batch_completed += 1
                        self.batch_current_shop_id = None
                        return self._advance_batch_locked()
        except Exception as exc:
            self._terminal_status = "browser_closed"
            self._terminal_detail = "The WAF browser window was closed or became unavailable"
            self.batch_active = False
            self._close()
            raise RuntimeError(self._terminal_detail) from exc
        self.challenge_attempts += 1
        # Reaching this method is itself proof that goodsList returned a WAF
        # challenge. Record cookies only after the real top-level navigation so
        # cookies created by loading the challenge are not mistaken for a solve.
        self._challenge_dom_seen = True
        self._challenge_cookie_baseline = self._cookie_fingerprint(cookies)
        self._challenge_observation = ""
        self._challenge_observation_count = 0
        self._challenge_ready = False
        self.automatic_status = self._attempt_automatic_slider(driver)
        attempted = self.automatic_status == DISPATCHED
        if attempted:
            detail = "Automatic WAF verification was submitted; confirming the result"
        elif self.automatic_status == FAILED:
            detail = "Automatic WAF verification did not complete; finish it in the open browser"
        else:
            detail = "Complete the WAF challenge in the open browser; synchronization will resume automatically"
        result = self._status(detail)
        result["automatic_attempted"] = attempted
        return result

    def _status(self, detail: str = "", status: str | None = None) -> dict[str, Any]:
        if status is None:
            status = self._terminal_status or (
                "ready" if self._challenge_ready else "awaiting_verification" if self.batch_active else "idle"
            )
        payload: dict[str, Any] = {
            "status": status,
            "detail": detail or self._terminal_detail,
            "browser": self.browser_name,
            "browser_mode": self.browser_mode,
            "proxy_mode": self.proxy_mode,
            "challenge_id": self.challenge_id,
            "challenge_attempts": self.challenge_attempts,
            "completion_replays": self._completion_replays,
            "automatic_status": self.automatic_status,
            "automatic_probe_count": self._automatic_probe_count,
            "automatic_attempted": self.automatic_status == DISPATCHED,
            "ready": status in {"ready", "success"},
            "observation_count": self._challenge_observation_count,
            "completed": self.batch_completed,
            "total": self.batch_total,
            "succeeded": sum(1 for entry in self.batch_results if entry.get("ok")),
            "failed": sum(1 for entry in self.batch_results if not entry.get("ok")),
            "current_shop_id": self.batch_current_shop_id,
            "pending_shop_ids": list(self.batch_queue),
            "results": list(self.batch_results),
        }
        if self.batch_total == 1 and self.batch_results and self.batch_results[-1].get("ok"):
            payload["summary"] = self.batch_results[-1].get("data")
        return payload

    def _shop(self, shop_id: int) -> Any:
        with self._database() as connection:
            shop = connection.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
        if shop is None:
            raise KeyError("monitored shop does not exist")
        return shop

    def _sync_shop(
        self,
        driver: Any,
        shop_id: int,
        *,
        first_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        shop = self._shop(shop_id)
        try:
            current_url = str(getattr(driver, "current_url", "") or "")
            page_source = str(getattr(driver, "page_source", "") or "")
        except Exception:
            current_url = ""
            page_source = ""
        redirected_challenge = self._same_origin(current_url, shop["url"]) and self._is_waf_html(page_source)
        if not self._same_top_level_url(current_url, shop["url"]) and not redirected_challenge:
            self._request_waiter()
            driver.get(shop["url"])
        payload = first_payload if first_payload is not None else self._browser_request(
            driver, self._request_data(shop, 1)
        )
        products = self._catalog(driver, shop, payload)
        with self._worker_lock:
            summary = self._record_shop_fetch(shop_id, products_override=products)
        self._publish_browser_state(driver, shop)
        return summary

    def _advance_batch_locked(self) -> dict[str, Any]:
        driver = self.driver
        if driver is None:
            raise RuntimeError("no active WAF browser session")
        while self.batch_queue:
            shop_id = self.batch_queue.pop(0)
            self.batch_current_shop_id = shop_id
            self.shop_id = shop_id
            try:
                summary = self._sync_shop(driver, shop_id)
            except self._waf_error as exc:
                self.batch_queue.insert(0, shop_id)
                return self._render_challenge(driver, str(exc))
            except Exception as exc:
                self.batch_results.append({"id": shop_id, "ok": False, "error": str(exc)[:240]})
                self.batch_completed += 1
                continue
            self.batch_results.append({"id": shop_id, "ok": True, "data": summary})
            self.batch_completed += 1
        self.batch_current_shop_id = None
        self.batch_active = False
        browser_name = self.browser_name
        self._close()
        failed = [entry for entry in self.batch_results if not entry.get("ok")]
        result = self._status(
            f"Browser synchronization completed: {self.batch_completed - len(failed)} succeeded, {len(failed)} failed",
            "success",
        )
        result["browser"] = browser_name
        self._last_completed_challenge_id = self.challenge_id
        self._last_completed_result = dict(result)
        return result

    def _begin_batch_locked(
        self,
        shop_ids: list[int],
        challenge_id: str | None = None,
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(int(value) for value in shop_ids))
        if not unique_ids:
            return {"status": "success", "detail": "No shops require WAF verification", "completed": 0, "total": 0, "results": []}
        requested_challenge_id = str(challenge_id or "").strip() or None
        if requested_challenge_id is not None and not self._CHALLENGE_ID.fullmatch(requested_challenge_id):
            raise RuntimeError("invalid WAF challenge id")
        if (
            not self.batch_active
            and requested_challenge_id == self._last_completed_challenge_id
            and self._last_completed_result is not None
        ):
            return dict(self._last_completed_result)
        if self.batch_active:
            if requested_challenge_id is not None and requested_challenge_id != self.challenge_id:
                raise RuntimeError("another WAF browser verification session is already active")
            if requested_challenge_id == self.challenge_id or tuple(unique_ids) == self.batch_shop_ids:
                return self._status("The existing WAF browser verification session is still active")
            raise RuntimeError("another WAF browser verification session is already active")

        shops = [self._shop(shop_id) for shop_id in unique_ids]
        self._close()
        self._reset_batch()
        self.batch_queue = unique_ids
        self.batch_shop_ids = tuple(unique_ids)
        self.batch_total = len(unique_ids)
        self.batch_active = True
        self.challenge_id = requested_challenge_id or uuid.uuid4().hex
        try:
            self.driver, self.browser_name = self._create_driver(str(shops[0]["url"]))
        except Exception:
            self._reset_batch()
            raise
        return self._advance_batch_locked()

    def start(self, shop_id: int, challenge_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            return self._begin_batch_locked([shop_id], challenge_id)

    def _require_active_locked(self, challenge_id: str | None = None) -> None:
        if challenge_id is not None and str(challenge_id) != self.challenge_id:
            raise RuntimeError("WAF browser verification session has changed")
        if self._terminal_status == "browser_closed":
            raise RuntimeError(self._terminal_detail or "The WAF browser window is closed")
        if not self.batch_active or self.driver is None:
            raise RuntimeError("no pending browser verification session")
        try:
            handles = getattr(self.driver, "window_handles", None)
            if handles is not None and not list(handles):
                raise RuntimeError("browser window is closed")
        except Exception as exc:
            self._terminal_status = "browser_closed"
            self._terminal_detail = "The WAF browser window was closed before verification completed"
            self.batch_active = False
            self._close()
            raise RuntimeError(self._terminal_detail) from exc

    def complete(self, shop_id: int, challenge_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            if (
                challenge_id is not None
                and str(challenge_id) == self._last_completed_challenge_id
                and self._last_completed_result is not None
                and not self.batch_active
            ):
                return dict(self._last_completed_result)
            self._require_active_locked(challenge_id)
            if self.batch_total != 1 or self.batch_current_shop_id != shop_id:
                raise RuntimeError("no pending browser verification session for this shop")
            if self._completion_replays >= self.MAX_COMPLETION_REPLAYS:
                self._terminal_status = "retry_exhausted"
                self._terminal_detail = "WAF verification retries were exhausted; start a new browser session"
                self.batch_active = False
                self._close()
                return self._status()
            self._completion_replays += 1
            # Completion is the only polling path that replays goodsList. One
            # WAF response returns immediately, so repeated challenges cannot
            # create an internal request loop.
            return self._advance_batch_locked()

    def start_all(self, shop_ids: list[int], challenge_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            return self._begin_batch_locked(shop_ids, challenge_id)

    def complete_all(self, challenge_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            if (
                challenge_id is not None
                and str(challenge_id) == self._last_completed_challenge_id
                and self._last_completed_result is not None
                and not self.batch_active
            ):
                return dict(self._last_completed_result)
            self._require_active_locked(challenge_id)
            if self._completion_replays >= self.MAX_COMPLETION_REPLAYS:
                self._terminal_status = "retry_exhausted"
                self._terminal_detail = "WAF verification retries were exhausted; start a new browser session"
                self.batch_active = False
                self._close()
                return self._status()
            self._completion_replays += 1
            return self._advance_batch_locked()

    def status(self, challenge_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            if (
                challenge_id is not None
                and str(challenge_id) == self._last_completed_challenge_id
                and self._last_completed_result is not None
                and not self.batch_active
            ):
                return dict(self._last_completed_result)
            if challenge_id is not None and self.challenge_id is not None and str(challenge_id) != self.challenge_id:
                raise RuntimeError("WAF browser verification session has changed")
            if self._terminal_status is not None:
                return self._status()
            if not self.batch_active:
                return self._status(status="idle")
            if self.driver is None or self.batch_current_shop_id is None:
                self._terminal_status = "browser_closed"
                self._terminal_detail = "The WAF browser window is no longer available"
                self.batch_active = False
                self._close()
                return self._status()
            try:
                current_url, page_source, cookies = self._browser_snapshot(self.driver)
                shop = self._shop(self.batch_current_shop_id)
            except Exception:
                self._terminal_status = "browser_closed"
                self._terminal_detail = "The WAF browser window was closed before verification completed"
                self.batch_active = False
                self._close()
                return self._status()

            page_cleared = (
                self._challenge_dom_seen
                and self._same_top_level_url(current_url, shop["url"])
                and not self._is_waf_html(page_source)
            )
            cookie_fingerprint = self._cookie_fingerprint(cookies)
            challenge_visible = self._challenge_is_visible(self.driver, page_source)
            if (
                not page_cleared
                and challenge_visible
                and self.automatic_status in {"not_started", NOT_READY}
                and self._automatic_probe_count < 3
            ):
                self.automatic_status = self._attempt_automatic_slider(self.driver)
            cookie_changed = (
                bool(cookie_fingerprint)
                and cookie_fingerprint != self._challenge_cookie_baseline
                and not challenge_visible
            )
            observation = (
                "page_cleared"
                if page_cleared
                else f"session_cookie_changed:{cookie_fingerprint}"
                if cookie_changed
                else ""
            )
            if observation:
                if observation == self._challenge_observation:
                    self._challenge_observation_count += 1
                else:
                    self._challenge_observation = observation
                    self._challenge_observation_count = 1
            else:
                self._challenge_observation = ""
                self._challenge_observation_count = 0
            self._challenge_ready = self._challenge_observation_count >= self.READY_OBSERVATIONS
            if self._challenge_ready:
                return self._status("Browser verification appears complete; confirm to synchronize", "ready")
            return self._status("Waiting for the browser challenge to complete", "awaiting_verification")

    def poll(self, challenge_id: str | None = None) -> dict[str, Any]:
        """Read browser state without navigating or calling the protected API."""
        return self.status(challenge_id)
