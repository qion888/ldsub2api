from __future__ import annotations

import re
import sqlite3
from typing import Any, Mapping


DEFAULT_INTERVAL = 60
DEFAULT_SHOP_INTERVAL = 300
MIN_INTERVAL = 1
MAX_INTERVAL = 86400


class MonitorNotFound(LookupError):
    pass


def normalize_interval(value: Any, default: int = DEFAULT_INTERVAL) -> int:
    return min(max(int(value or default), MIN_INTERVAL), MAX_INTERVAL)


def sync_shop_product_intervals(
    connection: sqlite3.Connection,
    shop_id: int,
    interval_seconds: int,
) -> int:
    cursor = connection.execute(
        """
        UPDATE watches SET interval_seconds = ? WHERE id IN (
            SELECT watch_id FROM shop_products WHERE shop_id = ? AND listed = 1
        )
        """,
        (interval_seconds, shop_id),
    )
    return cursor.rowcount


def update_watch_monitoring(
    connection: sqlite3.Connection,
    watch_id: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    enabled = 1 if bool(payload.get("enabled", True)) else 0
    interval = normalize_interval(payload.get("interval_seconds"))
    name = str(payload.get("name") or "").strip()[:100]
    cursor = connection.execute(
        "UPDATE watches SET name = ?, enabled = ?, interval_seconds = ? WHERE id = ?",
        (name, enabled, interval, watch_id),
    )
    if cursor.rowcount == 0:
        raise MonitorNotFound("监控商品不存在")
    return {
        "ok": True,
        "watch_id": watch_id,
        "enabled": bool(enabled),
        "interval_seconds": interval,
    }


def update_shop_monitoring(
    connection: sqlite3.Connection,
    shop_id: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    enabled = 1 if bool(payload.get("enabled", True)) else 0
    interval = normalize_interval(payload.get("interval_seconds"), DEFAULT_SHOP_INTERVAL)
    name = str(payload.get("name") or "").strip()[:100]
    keywords = str(payload.get("keywords") or "").strip()[:100]
    raw_category = payload.get("category_id")
    category_id = int(raw_category) if raw_category not in (None, "") else None
    category_name = str(payload.get("category_name") or "").strip()[:100] if category_id else ""
    goods_type = str(payload.get("goods_type") or "card").strip()[:30]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", goods_type):
        raise ValueError("商品类型格式无效")

    cursor = connection.execute(
        """
        UPDATE shops SET name = ?, keywords = ?, category_id = ?, category_name = ?, goods_type = ?,
            enabled = ?, interval_seconds = ? WHERE id = ?
        """,
        (name, keywords, category_id, category_name, goods_type, enabled, interval, shop_id),
    )
    if cursor.rowcount == 0:
        raise MonitorNotFound("监控店铺不存在")

    synced_product_count = sync_shop_product_intervals(connection, shop_id, interval)
    return {
        "ok": True,
        "shop_id": shop_id,
        "enabled": bool(enabled),
        "interval_seconds": interval,
        "synced_product_count": synced_product_count,
    }
