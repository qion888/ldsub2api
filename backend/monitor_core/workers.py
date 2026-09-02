"""Background scheduling for product, shop, and preorder monitoring."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable


class ShopBatchSyncInProgress(RuntimeError):
    """Raised when another all-shop synchronization is already running."""


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
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(name="product-monitor", daemon=True)
        self._database = database
        self._record_inventory_fetch = record_inventory_fetch
        self._record_shop_fetch = record_shop_fetch
        self._process_preorder = process_preorder
        self._mark_preorder_check_error = mark_preorder_check_error
        self._default_interval = default_interval
        self._sleeper = sleeper
        try:
            self._minimum_interval = max(60, int(minimum_interval))
        except (TypeError, ValueError):
            self._minimum_interval = 60
        self.stop_event = threading.Event()
        self.fetch_lock = threading.Lock()
        self._shop_batch_lock = threading.Lock()
        self._shop_batch_active = threading.Event()

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
        self._raise_if_shop_batch_active()
        with self.fetch_lock:
            self._raise_if_shop_batch_active()
            return self._record_inventory_fetch(watch_id)

    def fetch_many(self, watch_ids: list[int]) -> list[dict[str, Any]]:
        self._raise_if_shop_batch_active()
        with self.fetch_lock:
            self._raise_if_shop_batch_active()
            shop_refresh_cache: dict[int, dict[str, Any] | Exception] = {}
            results = []
            for watch_id in watch_ids:
                try:
                    results.append({"id": watch_id, "ok": True, "data": self._record_inventory_fetch(watch_id, shop_refresh_cache)})
                except Exception as exc:
                    results.append({"id": watch_id, "ok": False, "error": str(exc)[:240]})
            return results

    def fetch_shop(self, shop_id: int) -> dict[str, Any]:
        self._raise_if_shop_batch_active()
        with self.fetch_lock:
            self._raise_if_shop_batch_active()
            return self._record_shop_fetch(shop_id)

    def shop_batch_sync_in_progress(self) -> bool:
        return self._shop_batch_active.is_set()

    def _raise_if_shop_batch_active(self) -> None:
        if self.shop_batch_sync_in_progress():
            raise ShopBatchSyncInProgress("店铺批量同步正在进行，请稍后重试")

    def fetch_shops(self, shop_ids: list[int], interval_seconds: int) -> dict[str, Any]:
        """Synchronize shops serially with a protected gap between each shop."""
        if not self._shop_batch_lock.acquire(blocking=False):
            raise ShopBatchSyncInProgress("店铺批量同步正在进行")
        self._shop_batch_active.set()
        try:
            unique_ids = list(dict.fromkeys(int(shop_id) for shop_id in shop_ids))
            results: list[dict[str, Any]] = []
            with self.fetch_lock:
                for index, shop_id in enumerate(unique_ids):
                    if index:
                        self._sleeper(interval_seconds)
                    try:
                        data = self._record_shop_fetch(shop_id)
                        results.append({"id": shop_id, "ok": True, "data": data})
                    except Exception as exc:
                        results.append({"id": shop_id, "ok": False, "error": str(exc)[:240]})
            succeeded = sum(1 for result in results if result["ok"])
            return {
                "interval_seconds": interval_seconds,
                "total": len(results),
                "succeeded": succeeded,
                "failed": len(results) - succeeded,
                "results": results,
            }
        finally:
            self._shop_batch_active.clear()
            self._shop_batch_lock.release()
