"""Background scheduling for product, shop, and preorder monitoring."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable


class MonitorWorker(threading.Thread):
    def __init__(
        self,
        *,
        database: Callable[[], Any],
        record_inventory_fetch: Callable[..., dict[str, Any]],
        record_shop_fetch: Callable[..., dict[str, Any]],
        process_preorder: Callable[[int, dict[str, Any]], Any],
        mark_preorder_check_error: Callable[[int, Exception], None],
        default_interval: int,
        minimum_interval: int = 60,
    ) -> None:
        super().__init__(name="product-monitor", daemon=True)
        self._database = database
        self._record_inventory_fetch = record_inventory_fetch
        self._record_shop_fetch = record_shop_fetch
        self._process_preorder = process_preorder
        self._mark_preorder_check_error = mark_preorder_check_error
        self._default_interval = default_interval
        try:
            self._minimum_interval = max(60, int(minimum_interval))
        except (TypeError, ValueError):
            self._minimum_interval = 60
        self.stop_event = threading.Event()
        self.fetch_lock = threading.Lock()

    @staticmethod
    def _timestamp(value: Any) -> float:
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _interval(self, value: Any, fallback: int) -> int:
        try:
            requested = int(value or fallback)
        except (TypeError, ValueError):
            requested = fallback
        return max(self._minimum_interval, requested)

    def run(self) -> None:
        while not self.stop_event.wait(0.25):
            current = time.time()
            shop_refresh_cache: dict[int, dict[str, Any] | Exception] = {}
            with self._database() as connection:
                preorder_rows = connection.execute(
                    "SELECT id, watch_id, last_check, interval_seconds FROM preorders "
                    "WHERE enabled = 1 AND status = 'watching'"
                ).fetchall()
            for row in preorder_rows:
                if current - self._timestamp(row["last_check"]) < self._interval(row["interval_seconds"], self._minimum_interval):
                    continue
                if not self.fetch_lock.acquire(blocking=False):
                    break
                try:
                    product = self._record_inventory_fetch(row["watch_id"], shop_refresh_cache)
                    self._process_preorder(row["id"], product)
                except Exception as exc:
                    self._mark_preorder_check_error(row["id"], exc)
                finally:
                    self.fetch_lock.release()

            with self._database() as connection:
                rows = connection.execute(
                    "SELECT id, last_run, interval_seconds FROM watches WHERE enabled = 1"
                ).fetchall()
            for row in rows:
                if current - self._timestamp(row["last_run"]) < self._interval(row["interval_seconds"], self._default_interval):
                    continue
                if not self.fetch_lock.acquire(blocking=False):
                    break
                try:
                    self._record_inventory_fetch(row["id"], shop_refresh_cache)
                except Exception:
                    pass
                finally:
                    self.fetch_lock.release()

            with self._database() as connection:
                shop_rows = connection.execute(
                    "SELECT s.id, s.last_run, s.interval_seconds, "
                    "(SELECT error FROM shop_runs WHERE shop_id = s.id ORDER BY id DESC LIMIT 1) AS last_error "
                    "FROM shops s WHERE s.enabled = 1"
                ).fetchall()
            for row in shop_rows:
                retry_interval = self._interval(row["interval_seconds"], 300)
                if "waf" in str(row["last_error"] or "").lower():
                    retry_interval = max(retry_interval, 3600)
                if current - self._timestamp(row["last_run"]) < retry_interval or row["id"] in shop_refresh_cache:
                    continue
                if not self.fetch_lock.acquire(blocking=False):
                    break
                try:
                    shop_refresh_cache[row["id"]] = self._record_shop_fetch(row["id"])
                except Exception as exc:
                    shop_refresh_cache[row["id"]] = exc
                finally:
                    self.fetch_lock.release()

    def fetch(self, watch_id: int) -> dict[str, Any]:
        with self.fetch_lock:
            return self._record_inventory_fetch(watch_id)

    def fetch_many(self, watch_ids: list[int]) -> list[dict[str, Any]]:
        with self.fetch_lock:
            shop_refresh_cache: dict[int, dict[str, Any] | Exception] = {}
            results = []
            for watch_id in watch_ids:
                try:
                    results.append({"id": watch_id, "ok": True, "data": self._record_inventory_fetch(watch_id, shop_refresh_cache)})
                except Exception as exc:
                    results.append({"id": watch_id, "ok": False, "error": str(exc)[:240]})
            return results

    def fetch_shop(self, shop_id: int) -> dict[str, Any]:
        with self.fetch_lock:
            return self._record_shop_fetch(shop_id)
