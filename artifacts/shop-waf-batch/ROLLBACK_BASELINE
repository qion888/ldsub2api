"""Interactive Edge session used to complete storefront WAF verification."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable


class BrowserVerificationManager:
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
        self.lock = threading.Lock()
        self.driver: Any = None
        self.shop_id: int | None = None

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
        driver, self.driver, self.shop_id = self.driver, None, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def _browser_request(self, driver: Any, data: dict[str, Any]) -> dict[str, Any]:
        driver.set_script_timeout(30)
        result = driver.execute_async_script(
            """
            const payload = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch('/shopApi/Shop/goodsList', {
              method: 'POST',
              credentials: 'include',
              headers: {'Accept': 'application/json, text/plain, */*', 'Content-Type': 'application/json'},
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
            raise RuntimeError(str((result or {}).get("error") or "浏览器同步请求失败")[:200])
        text = str(result.get("text") or "")
        if self._is_waf_html(text):
            raise self._waf_error(text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("浏览器会话仍未返回商品 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("浏览器会话返回的商品数据格式无效")
        return payload

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
        try:
            driver.execute_script("document.open(); document.write(arguments[0]); document.close();", html_text)
        except Exception as exc:
            self._close()
            raise RuntimeError("无法在 Edge 中显示滑块验证页") from exc
        return {
            "status": "awaiting_verification",
            "detail": "请在已打开的 Edge 窗口完成滑块，然后点击“验证完成并同步”",
        }

    def start(self, shop_id: int) -> dict[str, Any]:
        with self.lock:
            with self._database() as connection:
                shop = connection.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
            if shop is None:
                raise KeyError("监控店铺不存在")
            self._close()
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
            except Exception as exc:
                raise RuntimeError("无法启动 Edge 浏览器验证会话") from exc
            self.driver, self.shop_id = driver, shop_id
            try:
                driver.get(shop["url"])
                first_payload = self._browser_request(driver, self._request_data(shop, 1))
                products = self._catalog(driver, shop, first_payload)
                with self._worker_lock:
                    summary = self._record_shop_fetch(shop_id, products_override=products)
                self._close()
                return {"status": "success", "summary": summary}
            except self._waf_error as exc:
                return self._render_challenge(driver, str(exc))
            except Exception as exc:
                self._close()
                raise RuntimeError(f"浏览器验证同步失败：{str(exc)[:160]}") from exc

    def complete(self, shop_id: int) -> dict[str, Any]:
        with self.lock:
            if self.driver is None or self.shop_id != shop_id:
                raise RuntimeError("没有等待完成的浏览器验证会话")
            driver = self.driver
            with self._database() as connection:
                shop = connection.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
            if shop is None:
                self._close()
                raise KeyError("监控店铺不存在")
            try:
                if self._is_waf_html(driver.page_source):
                    return {
                        "status": "awaiting_verification",
                        "detail": "滑块验证尚未完成，请在 Edge 窗口完成后重试",
                    }
                driver.get(shop["url"])
                first_payload = self._browser_request(driver, self._request_data(shop, 1))
                products = self._catalog(driver, shop, first_payload)
                with self._worker_lock:
                    summary = self._record_shop_fetch(shop_id, products_override=products)
                self._close()
                return {"status": "success", "summary": summary}
            except self._waf_error as exc:
                return self._render_challenge(driver, str(exc))
            except Exception as exc:
                self._close()
                raise RuntimeError(f"浏览器验证同步失败：{str(exc)[:160]}") from exc
