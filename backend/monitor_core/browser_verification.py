"""Interactive browser sessions used to complete storefront WAF verification."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

from .storefront import is_waf_response
from .windows_input import (
    DISPATCHED,
    FAILED,
    NOT_READY,
    attempt_native_slider,
    navigate_native_url,
    open_native_tab,
)


class BrowserVerificationManager:
    """Coordinate one persistent browser session across one or many shops.

    The browser keeps the challenge in the storefront origin. The UI can poll the
    local status method and resume the batch as soon as the challenge disappears,
    without replaying the protected catalog request while verification is pending.
    """

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
        self.lock = threading.RLock()
        self.driver: Any = None
        self.browser_process: subprocess.Popen[Any] | None = None
        self.shop_id: int | None = None
        self.browser_name: str | None = None
        self.automatic_attempted_shop_ids: set[int] = set()
        self.controller_window_handle: str | None = None
        self.challenge_window_handle: str | None = None
        self.challenge_revision = 0
        self.challenge_id: str | None = None
        self.challenge_shop_id: int | None = None
        self.automatic_status = "not_started"
        self._automatic_probe_count = 0
        self._completed_challenges: dict[str, dict[str, Any]] = {}
        self._ready_observation: tuple[str, str] | None = None
        self._ready_observation_count = 0
        self.batch_queue: list[int] = []
        self.batch_results: list[dict[str, Any]] = []
        self.batch_total = 0
        self.batch_completed = 0
        self.batch_current_shop_id: int | None = None
        self.batch_active = False

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
        encoded = text.encode("utf-8", "ignore")
        return any(marker in encoded for marker in self._waf_markers)

    def _close(self) -> None:
        driver, self.driver = self.driver, None
        browser_process, self.browser_process = self.browser_process, None
        self.shop_id = None
        self.browser_name = None
        self.controller_window_handle = None
        self.challenge_window_handle = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if browser_process is not None and browser_process.poll() is None:
            try:
                browser_process.terminate()
                browser_process.wait(timeout=5)
            except Exception:
                try:
                    browser_process.kill()
                except Exception:
                    pass

    def _reset_batch(self) -> None:
        self.automatic_attempted_shop_ids = set()
        self.batch_queue = []
        self.batch_results = []
        self.batch_total = 0
        self.batch_completed = 0
        self.batch_current_shop_id = None
        self.batch_active = False
        self.challenge_id = None
        self.challenge_shop_id = None
        self.automatic_status = "not_started"
        self._automatic_probe_count = 0
        self._reset_ready_observation()

    def _reset_ready_observation(self) -> None:
        self._ready_observation = None
        self._ready_observation_count = 0

    def _begin_challenge(self) -> None:
        self.challenge_revision += 1
        self.challenge_id = f"{self.challenge_revision}-{uuid.uuid4().hex}"
        self.challenge_shop_id = self.batch_current_shop_id
        self.automatic_status = "not_started"
        self._automatic_probe_count = 0
        self._reset_ready_observation()

    def _open_challenge_page(self, driver: Any, challenge_url: str) -> None:
        handles = list(getattr(driver, "window_handles", []) or [])
        old_handle = self.challenge_window_handle
        if old_handle and old_handle in handles and old_handle != self.controller_window_handle:
            driver.switch_to.window(old_handle)
            driver.close()
            handles = list(getattr(driver, "window_handles", []) or [])
        if self.controller_window_handle in handles:
            driver.switch_to.window(self.controller_window_handle)

        if sys.platform.startswith("win") and self.browser_process is not None:
            native_handle: str | None = None
            try:
                native_handle = open_native_tab(driver, self.browser_process)
                if native_handle:
                    driver.switch_to.window(native_handle)
                    self._install_browser_compatibility(driver)
                    if navigate_native_url(self.browser_process, challenge_url):
                        self.challenge_window_handle = native_handle
                        return
            except Exception:
                pass
            if native_handle:
                try:
                    if native_handle in list(driver.window_handles):
                        driver.switch_to.window(native_handle)
                        driver.close()
                except Exception:
                    pass
            handles = list(getattr(driver, "window_handles", []) or [])
            if self.controller_window_handle in handles:
                driver.switch_to.window(self.controller_window_handle)

        new_handle: str | None = None
        if hasattr(driver, "execute_cdp_cmd"):
            existing_handles = set(handles)
            target = driver.execute_cdp_cmd("Target.createTarget", {"url": "about:blank"})
            target_id = str((target or {}).get("targetId") or "")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                handles = list(driver.window_handles)
                added_handles = [handle for handle in handles if handle not in existing_handles]
                matching_handle = next(
                    (
                        handle
                        for handle in added_handles
                        if target_id and (handle == target_id or handle.endswith(target_id))
                    ),
                    None,
                )
                if matching_handle or len(added_handles) == 1:
                    new_handle = matching_handle or added_handles[0]
                    break
                time.sleep(0.05)
            if not new_handle:
                raise RuntimeError("challenge browser tab did not open")
        elif hasattr(getattr(driver, "switch_to", None), "new_window"):
            driver.switch_to.new_window("tab")
            new_handle = str(driver.current_window_handle)

        if new_handle:
            driver.switch_to.window(new_handle)
        self._install_browser_compatibility(driver)
        driver.get(challenge_url)
        self.challenge_window_handle = str(
            getattr(driver, "current_window_handle", "") or new_handle or "__current__"
        )

    def _activate_challenge_window(self) -> bool:
        if self.driver is None or not self.challenge_window_handle:
            return False
        if self.challenge_window_handle == "__current__":
            return True
        try:
            handles = list(self.driver.window_handles)
            if self.challenge_window_handle not in handles:
                return False
            self.driver.switch_to.window(self.challenge_window_handle)
            return True
        except Exception:
            return False

    def _remember_challenge_result(self, challenge_id: str, result: dict[str, Any]) -> None:
        self._completed_challenges[challenge_id] = dict(result)
        while len(self._completed_challenges) > 32:
            self._completed_challenges.pop(next(iter(self._completed_challenges)))

    @staticmethod
    def _terminate_browser_process(process: Any) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

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

    @staticmethod
    def _chromium_binary(browser: str) -> Path | None:
        configured = os.environ.get("LDXP_WAF_BROWSER_BINARY", "").strip()
        if configured:
            path = Path(configured).expanduser()
            return path if path.is_file() else None
        if sys.platform.startswith("win"):
            roots = [
                Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")),
                Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")),
                Path(os.environ.get("LOCALAPPDATA", "")),
            ]
            relative = (
                [Path("Microsoft/Edge/Application/msedge.exe")]
                if browser == "edge"
                else [Path("Google/Chrome/Application/chrome.exe"), Path("Chromium/Application/chrome.exe")]
            )
            candidates = [root / suffix for root in roots for suffix in relative if str(root)]
        elif sys.platform == "darwin":
            candidates = (
                [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
                if browser == "edge"
                else [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
            )
        else:
            names = {
                "edge": ("/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"),
                "chrome": ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"),
                "chromium": ("/usr/bin/chromium", "/usr/bin/chromium-browser"),
            }
            candidates = [Path(value) for value in names.get(browser, ())]
        return next((path for path in candidates if path.is_file()), None)

    @staticmethod
    def _available_debug_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def _browser_compatibility_script() -> str:
        return """
        for (const key of Object.getOwnPropertyNames(window)) {
          if (/^cdc_[A-Za-z0-9]+_(Array|Object|Promise|Proxy|Symbol|JSON|Window)$/.test(key)) {
            try { delete window[key]; } catch (_) {}
          }
        }
        if (navigator.webdriver) {
          try { Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined, configurable: true}); } catch (_) {}
        }
        """

    def _install_browser_compatibility(self, driver: Any) -> None:
        if not hasattr(driver, "execute_cdp_cmd"):
            return
        source = self._browser_compatibility_script()
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        try:
            driver.execute_script(source)
        except Exception:
            pass

    @staticmethod
    def _prepare_attached_window(driver: Any) -> None:
        existing_handles = set(driver.window_handles)
        target = driver.execute_cdp_cmd("Target.createTarget", {"url": "about:blank"})
        target_id = str((target or {}).get("targetId") or "")
        deadline = time.monotonic() + 5
        new_handle: str | None = None
        while time.monotonic() < deadline:
            added_handles = [handle for handle in driver.window_handles if handle not in existing_handles]
            matching_handle = next(
                (
                    handle
                    for handle in added_handles
                    if target_id and (handle == target_id or handle.endswith(target_id))
                ),
                None,
            )
            if matching_handle or len(added_handles) == 1:
                new_handle = matching_handle or added_handles[0]
                break
            time.sleep(0.05)
        if not new_handle:
            raise RuntimeError("attached browser did not provide a stable page")
        driver.switch_to.window(new_handle)

    def _create_attached_chromium_driver(self, webdriver: Any, options_type: Any, browser: str) -> Any:
        binary = self._chromium_binary(browser)
        if binary is None:
            raise RuntimeError(f"{browser} browser binary was not found")
        port = self._available_debug_port()
        command = [
            str(binary),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self._profile_path}",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--new-window",
            "about:blank",
        ]
        if browser == "edge":
            command.append("--disable-features=EdgeFirstRunExperience")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 12
        while process.poll() is None and time.monotonic() < deadline:
            try:
                with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1):
                    break
            except Exception:
                time.sleep(0.1)
        else:
            self._terminate_browser_process(process)
            raise RuntimeError(f"{browser} debugging endpoint did not start")
        options = options_type()
        options.debugger_address = f"127.0.0.1:{port}"
        driver: Any = None
        try:
            driver = webdriver.Edge(options=options) if browser == "edge" else webdriver.Chrome(options=options)
            self._prepare_attached_window(driver)
            self._install_browser_compatibility(driver)
        except Exception:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            self._terminate_browser_process(process)
            raise
        self.browser_process = process
        return driver

    def _create_driver(self) -> tuple[Any, str]:
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
        errors: list[str] = []
        for browser in self._browser_order():
            try:
                if browser == "edge":
                    driver = self._create_attached_chromium_driver(webdriver, EdgeOptions, browser)
                elif browser in {"chrome", "chromium"}:
                    driver = self._create_attached_chromium_driver(webdriver, ChromeOptions, browser)
                else:
                    options = FirefoxOptions()
                    options.add_argument("-profile")
                    options.add_argument(str(self._profile_path))
                    # Firefox otherwise defaults to a direct connection. Type 5
                    # inherits the operating-system VPN/proxy configuration.
                    options.set_preference("network.proxy.type", 5)
                    driver = webdriver.Firefox(options=options)
                return driver, browser
            except Exception as exc:
                errors.append(f"{browser}: {str(exc)[:120]}")
        detail = "; ".join(errors) or "no browser candidates"
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            detail += "; Linux needs a graphical session (DISPLAY/WAYLAND_DISPLAY) for the interactive challenge"
        raise RuntimeError(f"Unable to start a WAF browser ({detail})")

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
        enabled = os.environ.get("LDXP_WAF_AUTO_VERIFY", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        shop_id = self.batch_current_shop_id
        if not enabled:
            return "disabled"
        if shop_id is None or shop_id in self.automatic_attempted_shop_ids:
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

    def _render_challenge(self, driver: Any, _html_text: str) -> dict[str, Any]:
        # Aliyun binds the challenge to a real top-level navigation. Injecting an
        # XHR response with document.write creates a page that looks valid but is
        # rejected after the slider is released.
        if self.batch_current_shop_id is None:
            raise RuntimeError("Unable to display the WAF challenge in the browser")
        try:
            result = self._start_challenge_locked(driver)
        except Exception as exc:
            self._close()
            self._reset_batch()
            raise RuntimeError("Unable to display the WAF challenge in the browser") from exc
        return result

    def _start_challenge_locked(self, driver: Any) -> dict[str, Any]:
        if self.batch_current_shop_id is None:
            raise RuntimeError("no shop is waiting for browser verification")
        shop = self._shop(self.batch_current_shop_id)
        challenge_url = urljoin(str(shop["url"]), "/shopApi/Shop/goodsList")
        self._request_waiter()
        self._open_challenge_page(driver, challenge_url)
        self._begin_challenge()
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
            status = "awaiting_verification" if self.batch_active else "success"
        payload: dict[str, Any] = {
            "status": status,
            "detail": detail,
            "browser": self.browser_name,
            "completed": self.batch_completed,
            "total": self.batch_total,
            "succeeded": sum(1 for entry in self.batch_results if entry.get("ok")),
            "failed": sum(1 for entry in self.batch_results if not entry.get("ok")),
            "current_shop_id": self.batch_current_shop_id,
            "challenge_id": self.challenge_id,
            "challenge_revision": self.challenge_revision if self.challenge_id else None,
            "automatic_status": self.automatic_status,
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

    def _sync_shop(self, driver: Any, shop_id: int) -> dict[str, Any]:
        shop = self._shop(shop_id)
        self._request_waiter()
        driver.get(shop["url"])
        first_payload = self._browser_request(driver, self._request_data(shop, 1))
        products = self._catalog(driver, shop, first_payload)
        with self._worker_lock:
            return self._record_shop_fetch(shop_id, products_override=products)

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
            self.challenge_id = None
            self.challenge_shop_id = None
            self._reset_ready_observation()
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
        return result

    def _begin_batch_locked(self, shop_ids: list[int]) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(int(value) for value in shop_ids))
        if not unique_ids:
            return {"status": "success", "detail": "No shops require WAF verification", "completed": 0, "total": 0, "results": []}
        for shop_id in unique_ids:
            self._shop(shop_id)
        self._close()
        self._reset_batch()
        self.batch_queue = unique_ids
        self.batch_total = len(unique_ids)
        self.batch_active = True
        try:
            self.driver, self.browser_name = self._create_driver()
            current_handle = getattr(self.driver, "current_window_handle", None)
            self.controller_window_handle = str(current_handle) if current_handle else None
        except Exception:
            self._reset_batch()
            raise
        return self._advance_batch_locked()

    def start(self, shop_id: int) -> dict[str, Any]:
        with self.lock:
            return self._begin_batch_locked([shop_id])

    def _validate_challenge(self, challenge_id: str | None) -> dict[str, Any] | None:
        if not challenge_id:
            raise RuntimeError("challenge_id is required")
        if challenge_id and challenge_id in self._completed_challenges:
            return dict(self._completed_challenges[challenge_id])
        if challenge_id and challenge_id != self.challenge_id:
            raise RuntimeError("browser verification challenge is stale")
        return None

    def complete(self, shop_id: int, challenge_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            completed = self._validate_challenge(challenge_id)
            if completed is not None:
                return completed
            if not self.batch_active or self.batch_total != 1 or self.batch_current_shop_id != shop_id or self.driver is None:
                raise RuntimeError("no pending browser verification session for this shop")
            # Re-run the catalog request as the source of truth. Challenge pages
            # can retain WAF markers after the slider has already issued a cookie.
            result = self._advance_batch_locked()
            if challenge_id:
                self._remember_challenge_result(challenge_id, result)
            return result

    def reopen(self, challenge_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            completed = self._validate_challenge(challenge_id)
            if completed is not None:
                return completed
            if not self.batch_active or self.driver is None or self.batch_current_shop_id is None:
                raise RuntimeError("no pending browser verification session")
            previous_id = str(challenge_id)
            try:
                result = self._start_challenge_locked(self.driver)
            except Exception as exc:
                self._close()
                self._reset_batch()
                raise RuntimeError("Unable to reopen the WAF verification page") from exc
            self._remember_challenge_result(previous_id, result)
            return result

    def start_all(self, shop_ids: list[int]) -> dict[str, Any]:
        with self.lock:
            return self._begin_batch_locked(shop_ids)

    def complete_all(self, challenge_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            completed = self._validate_challenge(challenge_id)
            if completed is not None:
                return completed
            if not self.batch_active or self.driver is None:
                raise RuntimeError("no pending batch browser verification session")
            # Re-run the catalog request as the source of truth. Challenge pages
            # can retain WAF markers after the slider has already issued a cookie.
            result = self._advance_batch_locked()
            if challenge_id:
                self._remember_challenge_result(challenge_id, result)
            return result

    def _page_observation(self) -> dict[str, Any]:
        if self.driver is None or self.batch_current_shop_id is None:
            return {"ready": False, "reason": "missing_session"}
        if not self._activate_challenge_window():
            return {"ready": False, "reason": "navigation"}
        try:
            page_source = str(self.driver.page_source or "")
            current_url = str(self.driver.current_url or "")
            page_state = self.driver.execute_script(
                """
                const slider = document.querySelector('#aliyunCaptcha-sliding-slider');
                const rect = slider ? slider.getBoundingClientRect() : null;
                const style = slider ? getComputedStyle(slider) : null;
                const failed = Boolean(document.querySelector(
                  '#aliyunCaptcha-sliding-left.fail, .aliyunCaptcha-sliding-fail, '
                  + '.aliyunCaptcha-fail, [class*="captcha"][class*="fail"]'
                ));
                const bodyText = document.body?.innerText || '';
                const success = Boolean(document.querySelector(
                  '#aliyunCaptcha-sliding-left.success, .aliyunCaptcha-sliding-success, '
                  + '.aliyunCaptcha-success, [class*="captcha"][class*="success"]'
                )) || /\u9a8c\u8bc1(?:\u901a\u8fc7|\u6210\u529f)/.test(bodyText);
                return {
                  ready_state: document.readyState,
                  slider_hidden: Boolean(slider) && (!rect || rect.width <= 0 || rect.height <= 0 ||
                    style.display === 'none' || style.visibility === 'hidden'),
                  failed,
                  success
                };
                """
            )
        except Exception:
            return {"ready": False, "reason": "navigation"}
        state = page_state if isinstance(page_state, dict) else {}
        if state.get("failed"):
            return {"ready": False, "reason": "failed"}
        if not page_source.strip() or str(state.get("ready_state") or "") != "complete":
            return {"ready": False, "reason": "navigation"}
        if self._is_waf_html(page_source) and not state.get("success"):
            return {"ready": False, "reason": "hidden" if state.get("slider_hidden") else "challenge"}
        try:
            expected_url = str(self._shop(self.batch_current_shop_id)["url"])
            expected = urlsplit(expected_url)
            current = urlsplit(current_url)
        except Exception:
            return {"ready": False, "reason": "navigation"}
        if current.scheme not in {"http", "https"} or (current.scheme, current.netloc) != (expected.scheme, expected.netloc):
            return {"ready": False, "reason": "navigation"}
        protected_path = urlsplit(urljoin(expected_url, "/shopApi/Shop/goodsList")).path.rstrip("/")
        if current.path.rstrip("/") != protected_path and not state.get("success"):
            return {"ready": False, "reason": "unexpected_page"}
        key = (current_url, f"{state.get('ready_state')}:{bool(state.get('success'))}")
        return {"ready": True, "reason": "clear", "key": key}

    def status(self) -> dict[str, Any]:
        with self.lock:
            if not self.batch_active:
                return {
                    "status": "idle",
                    "browser": self.browser_name,
                    "completed": self.batch_completed,
                    "total": self.batch_total,
                    "succeeded": sum(1 for entry in self.batch_results if entry.get("ok")),
                    "failed": sum(1 for entry in self.batch_results if not entry.get("ok")),
                    "current_shop_id": None,
                    "challenge_id": None,
                    "challenge_revision": None,
                    "automatic_status": self.automatic_status,
                    "pending_shop_ids": list(self.batch_queue),
                    "results": list(self.batch_results),
                }
            if self.driver is None:
                raise RuntimeError("no active WAF browser session")
            observation = self._page_observation()
            reason = observation.get("reason")
            if (
                reason == "challenge"
                and self.automatic_status in {"not_started", NOT_READY}
                and self._automatic_probe_count < 3
            ):
                self.automatic_status = self._attempt_automatic_slider(self.driver)
            if observation.get("ready"):
                key = observation["key"]
                if key == self._ready_observation:
                    self._ready_observation_count += 1
                else:
                    self._ready_observation = key
                    self._ready_observation_count = 1
            else:
                self._reset_ready_observation()
            ready = self._ready_observation_count >= 2
            if reason == "failed" and self.automatic_status == DISPATCHED:
                self.automatic_status = FAILED
            elif ready and self.automatic_status == DISPATCHED:
                self.automatic_status = "passed"
            if reason == "failed":
                detail = "WAF verification failed; use the system action to open a new verification page"
            elif reason == "hidden":
                detail = "WAF slider is unavailable; use the system action to open a new verification page"
            elif observation.get("ready") and not ready:
                detail = "Verification result detected; confirming browser state"
            elif reason == "navigation":
                detail = "Waiting for the verification page to finish loading"
            elif reason == "unexpected_page":
                detail = "Waiting for the protected verification page"
            elif self.automatic_status == FAILED:
                detail = "Automatic WAF verification failed; use the system action to open a new verification page"
            elif self.automatic_status == DISPATCHED:
                detail = "Automatic WAF verification was submitted; confirming the result"
            else:
                detail = "Waiting for browser WAF verification"
            action_required = reason in {"failed", "hidden"} or self.automatic_status == FAILED
            return self._status(
                "Verification complete; ready to resume synchronization"
                if ready else detail,
                "ready" if ready else "action_required" if action_required else "awaiting_verification",
            )
