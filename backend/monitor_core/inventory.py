"""Product snapshots, shop synchronization, and inventory projections."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable


class InventoryService:
    def __init__(
        self,
        *,
        database: Callable[[], Any],
        now: Callable[[], str],
        fetch_goods: Callable[[str], dict[str, Any]],
        fetch_shop_catalog: Callable[..., list[dict[str, Any]]],
        commerce_tags: Callable[[dict[str, Any]], list[dict[str, str]]],
        is_unlisted_error: Callable[[Any], bool],
        sync_intervals: Callable[[Any, int, int], int],
    ) -> None:
        self.database = database
        self.now = now
        self.fetch_goods = fetch_goods
        self.fetch_shop_catalog = fetch_shop_catalog
        self.commerce_tags = commerce_tags
        self.is_unlisted_error = is_unlisted_error
        self.sync_intervals = sync_intervals

    @staticmethod
    def _json_value(value: str | None, fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback


    def serialize_snapshot(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["specs"] = self._json_value(result.get("specs"), {})
        result["raw_data"] = self._json_value(result.get("raw_data"), {})
        raw_stock = result.get("stock")
        result["stock"] = int(raw_stock) if str(raw_stock or "").isdigit() else None
        result["stock_label"] = str(raw_stock) if raw_stock not in (None, "") else "接口未公开数量"
        if result.get("sale_status") == "off_sale":
            result["stock"] = None
            result["stock_label"] = "未上架"
        result["query_password_required"] = result["specs"].get("查询密码") == "需要"
        legacy_limit = result["specs"].pop("单次限购", None)
        if "最低起购" not in result["specs"] and legacy_limit not in (None, ""):
            result["specs"]["最低起购"] = legacy_limit
        limit_value = result["specs"].get("最低起购")
        result["limit_count"] = int(limit_value) if str(limit_value).isdigit() else None
        commerce_source = dict(result["raw_data"])
        commerce_extend = commerce_source.get("extend") if isinstance(commerce_source.get("extend"), dict) else {}
        commerce_extend = dict(commerce_extend)
        if result["limit_count"] is not None:
            commerce_extend.setdefault("limit_count", result["limit_count"])
        if result["query_password_required"]:
            commerce_extend.setdefault("query_password_status", 1)
        commerce_source["extend"] = commerce_extend
        result["commerce_tags"] = self.commerce_tags(commerce_source)
        return result


    def effective_watch_product(
        self,
        connection: sqlite3.Connection,
        watch_id: int,
        latest: sqlite3.Row | None = None,
        attempt: sqlite3.Row | None = None,
    ) -> dict[str, Any] | None:
        if latest is None:
            latest = connection.execute(
                "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                (watch_id,),
            ).fetchone()
        product = self.serialize_snapshot(latest)
        if product is None:
            return None
        if attempt is None:
            attempt = connection.execute(
                "SELECT fetched_at, status, error FROM snapshots WHERE watch_id = ? ORDER BY id DESC LIMIT 1",
                (watch_id,),
            ).fetchone()
        shop_state = connection.execute(
            """
            SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN listed = 1 THEN 1 ELSE 0 END), 0) AS listed
            FROM shop_products WHERE watch_id = ?
            """,
            (watch_id,),
        ).fetchone()
        removed_from_catalog = bool(shop_state and shop_state["total"] and not shop_state["listed"])
        explicit_unlisted = bool(
            attempt and attempt["status"] == "error" and self.is_unlisted_error(attempt["error"])
        )
        if product.get("sale_status") != "on_sale" or explicit_unlisted or removed_from_catalog:
            product["sale_status"] = "off_sale"
            product["stock"] = None
            product["stock_label"] = "未上架"
        return product

    def _link_discovered_shop(
        self,
        connection: sqlite3.Connection,
        watch_id: int,
        product: dict[str, Any],
        stamp: str,
    ) -> dict[str, Any]:
        """Persist a shop exposed by a single-product detail response."""
        shop_info = product.get("shop")
        if not isinstance(shop_info, dict) or not shop_info.get("token"):
            return {"status": "unavailable", "shop": None}

        token = str(shop_info["token"]).strip()
        url = str(shop_info.get("url") or f"https://pay.ldxp.cn/shop/{token}").strip()
        name = str(shop_info.get("name") or token).strip()[:100] or token
        category_id = shop_info.get("category_id")
        category_name = str(shop_info.get("category_name") or "").strip()[:100]
        goods_type = str(shop_info.get("goods_type") or "card").strip()[:30] or "card"

        shop = connection.execute(
            "SELECT id, name, category_id, category_name, goods_type FROM shops WHERE token = ? OR url = ?",
            (token, url),
        ).fetchone()
        if shop is None:
            connection.execute(
                """
                INSERT OR IGNORE INTO shops(
                    url, token, name, keywords, category_id, category_name, goods_type,
                    enabled, interval_seconds, created_at
                ) VALUES(?, ?, ?, '', ?, ?, ?, 1, 300, ?)
                """,
                (url, token, name, category_id, category_name, goods_type, stamp),
            )
            shop = connection.execute(
                "SELECT id, name, category_id, category_name, goods_type FROM shops WHERE token = ? OR url = ?",
                (token, url),
            ).fetchone()
            if shop is None:
                return {"status": "unavailable", "shop": None}
            shop_id = int(shop["id"])
            shop_name = str(shop["name"] or "").strip() or name
        else:
            shop_id = int(shop["id"])
            shop_name = str(shop["name"] or "").strip() or name
            # Fill metadata discovered from the product without replacing a
            # name/category that an operator has already configured.
            connection.execute(
                """
                UPDATE shops SET
                    category_id = COALESCE(category_id, ?),
                    category_name = CASE WHEN category_name IS NULL OR category_name = '' THEN ? ELSE category_name END,
                    goods_type = CASE WHEN goods_type IS NULL OR goods_type = '' THEN ? ELSE goods_type END
                WHERE id = ?
                """,
                (category_id, category_name, goods_type, shop_id),
            )

        connection.execute(
            "UPDATE shops SET name = ? WHERE id = ? AND (name IS NULL OR name = '' OR name = token)",
            (name, shop_id),
        )

        connection.execute(
            """
            INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen)
            VALUES(?, ?, ?, 1, ?)
            ON CONFLICT(shop_id, goods_key) DO UPDATE SET
                watch_id = excluded.watch_id,
                listed = 1,
                last_seen = excluded.last_seen
            """,
            (shop_id, product["goods_key"], watch_id, stamp),
        )
        connection.execute(
            "DELETE FROM shop_exclusions WHERE shop_id = ? AND goods_key = ?",
            (shop_id, product["goods_key"]),
        )
        shop_interval = connection.execute(
            "SELECT interval_seconds FROM shops WHERE id = ?", (shop_id,)
        ).fetchone()
        if shop_interval is not None:
            self.sync_intervals(connection, shop_id, int(shop_interval["interval_seconds"] or 300))
        shop_row = connection.execute(
            "SELECT id, name, token FROM shops WHERE id = ?", (shop_id,)
        ).fetchone()
        linked_shop = dict(shop_row) if shop_row else {
            "id": shop_id,
            "name": shop_name,
            "token": token,
        }
        return {"status": "linked", "shop": linked_shop}


    def record_fetch(self, watch_id: int) -> dict[str, Any]:
        stamp = self.now()
        with self.database() as connection:
            watch = connection.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        if watch is None:
            raise KeyError("监控商品不存在")
        try:
            data = self.fetch_goods(watch["url"])
            with self.database() as connection:
                previous = connection.execute(
                    "SELECT price FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                    (watch_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO snapshots(
                        watch_id, title, price, market_price, stock, description, specs,
                        image, sale_status, goods_key, raw_data, fetched_at, status, error
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', NULL)
                    """,
                    (
                        watch_id,
                        data["title"],
                        data["price"],
                        data["market_price"],
                        None if data["stock"] is None else str(data["stock"]),
                        data["description"],
                        json.dumps(data["specs"], ensure_ascii=False),
                        data["image"],
                        data["sale_status"],
                        data["goods_key"],
                        json.dumps(data["raw_data"], ensure_ascii=False),
                        stamp,
                    ),
                )
                discovery = self._link_discovered_shop(connection, watch_id, data, stamp)
                connection.execute("UPDATE watches SET last_run = ? WHERE id = ?", (stamp, watch_id))
            old_price = previous["price"] if previous else None
            data["previous_price"] = old_price
            data["price_changed"] = old_price not in (None, "") and old_price != data["price"]
            data.update({"fetched_at": stamp, "status": "success"})
            data["shop_discovery"] = discovery
            data.pop("raw_data", None)
            return data
        except Exception as exc:
            error = str(exc)[:240] or "抓取失败"
            with self.database() as connection:
                connection.execute(
                    "INSERT INTO snapshots(watch_id, fetched_at, status, error) VALUES(?, ?, 'error', ?)",
                    (watch_id, stamp, error),
                )
                connection.execute("UPDATE watches SET last_run = ? WHERE id = ?", (stamp, watch_id))
            raise RuntimeError(error) from exc


    def record_shop_fetch(self, shop_id: int, products_override: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        stamp = self.now()
        with self.database() as connection:
            shop = connection.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
        if shop is None:
            raise KeyError("监控店铺不存在")
        try:
            products = products_override
            if products is None:
                products = self.fetch_shop_catalog(
                    shop["url"],
                    keywords=shop["keywords"] or "",
                    category_id=shop["category_id"],
                    goods_type=shop["goods_type"] or "card",
                )
            with self.database() as connection:
                connection.execute("UPDATE shop_products SET listed = 0 WHERE shop_id = ?", (shop_id,))
                excluded = {
                    row["goods_key"]
                    for row in connection.execute(
                        "SELECT goods_key FROM shop_exclusions WHERE shop_id = ?", (shop_id,)
                    )
                }
                imported_count = 0
                for product in products:
                    if product["goods_key"] in excluded:
                        continue
                    imported_count += 1
                    connection.execute(
                        """
                        INSERT INTO watches(url, name, enabled, interval_seconds, created_at)
                        VALUES(?, ?, 0, ?, ?)
                        ON CONFLICT(url) DO NOTHING
                        """,
                        (product["source_url"], product["title"], shop["interval_seconds"], stamp),
                    )
                    watch = connection.execute(
                        "SELECT id FROM watches WHERE url = ?", (product["source_url"],)
                    ).fetchone()
                    watch_id = watch["id"]
                    connection.execute(
                        """
                        INSERT INTO snapshots(
                            watch_id, title, price, market_price, stock, description, specs,
                            image, sale_status, goods_key, raw_data, fetched_at, status, error
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', NULL)
                        """,
                        (
                            watch_id,
                            product["title"],
                            product["price"],
                            product["market_price"],
                            None if product["stock"] in (None, "") else str(product["stock"]),
                            product["description"],
                            json.dumps(product["specs"], ensure_ascii=False),
                            product["image"],
                            product["sale_status"],
                            product["goods_key"],
                            json.dumps(product["raw_data"], ensure_ascii=False),
                            stamp,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO shop_products(shop_id, goods_key, watch_id, listed, last_seen)
                        VALUES(?, ?, ?, 1, ?)
                        ON CONFLICT(shop_id, goods_key) DO UPDATE SET
                            watch_id = excluded.watch_id,
                            listed = 1,
                            last_seen = excluded.last_seen
                        """,
                        (shop_id, product["goods_key"], watch_id, stamp),
                    )
                connection.execute(
                    "INSERT INTO shop_runs(shop_id, fetched_at, status, product_count) VALUES(?, ?, 'success', ?)",
                    (shop_id, stamp, imported_count),
                )
                detected_name = next(
                    (
                        str((product["raw_data"].get("user") or {}).get("nickname") or "").strip()
                        for product in products
                        if isinstance(product.get("raw_data", {}).get("user"), dict)
                        and str((product["raw_data"].get("user") or {}).get("nickname") or "").strip()
                    ),
                    "",
                )
                if detected_name:
                    connection.execute(
                        "UPDATE shops SET name = ? WHERE id = ? AND (name IS NULL OR name = '' OR name = token)",
                        (detected_name[:100], shop_id),
                    )
                connection.execute("UPDATE shops SET last_run = ? WHERE id = ?", (stamp, shop_id))
                connection.execute(
                    """
                    UPDATE watches SET last_run = ? WHERE id IN (
                        SELECT watch_id FROM shop_products WHERE shop_id = ? AND listed = 1
                    )
                    """,
                    (stamp, shop_id),
                )
                self.sync_intervals(connection, shop_id, int(shop["interval_seconds"]))
            imported_products = [product for product in products if product["goods_key"] not in excluded]
            known_stock = [product["stock"] for product in imported_products if product["stock"] not in (None, "")]
            return {
                "status": "success",
                "fetched_at": stamp,
                "product_count": len(imported_products),
                "known_stock_count": len(known_stock),
                "on_sale_count": sum(product["sale_status"] == "on_sale" for product in imported_products),
            }
        except Exception as exc:
            error = str(exc)[:240] or "店铺抓取失败"
            with self.database() as connection:
                connection.execute(
                    "INSERT INTO shop_runs(shop_id, fetched_at, status, error) VALUES(?, ?, 'error', ?)",
                    (shop_id, stamp, error),
                )
                connection.execute("UPDATE shops SET last_run = ? WHERE id = ?", (stamp, shop_id))
            raise RuntimeError(error) from exc


    def _watch_inventory_shop_id(self, watch_id: int) -> int | None:
        with self.database() as connection:
            watch = connection.execute("SELECT 1 FROM watches WHERE id = ?", (watch_id,)).fetchone()
            if watch is None:
                raise KeyError("监控商品不存在")
            shop = connection.execute(
                """
                SELECT s.id FROM shops s
                JOIN shop_products sp ON sp.shop_id = s.id
                WHERE sp.watch_id = ? AND sp.listed = 1
                ORDER BY s.enabled DESC, s.id
                LIMIT 1
                """,
                (watch_id,),
            ).fetchone()
        return int(shop["id"]) if shop else None


    def _latest_watch_product(self, watch_id: int, *, shop_id: int, shop_summary: dict[str, Any]) -> dict[str, Any]:
        with self.database() as connection:
            linked = connection.execute(
                "SELECT 1 FROM shop_products WHERE shop_id = ? AND watch_id = ? AND listed = 1",
                (shop_id, watch_id),
            ).fetchone()
            snapshots = connection.execute(
                """
                SELECT * FROM snapshots
                WHERE watch_id = ? AND status = 'success'
                ORDER BY id DESC LIMIT 2
                """,
                (watch_id,),
            ).fetchall()
        if linked is None or not snapshots:
            raise RuntimeError("店铺同步完成，但该商品已不在店铺列表")
        product = self.serialize_snapshot(snapshots[0]) or {}
        previous_price = snapshots[1]["price"] if len(snapshots) > 1 else None
        product["previous_price"] = previous_price
        product["price_changed"] = (
            previous_price not in (None, "") and previous_price != product.get("price")
        )
        product["refresh_source"] = "shop"
        product["shop_id"] = shop_id
        product["shop_summary"] = shop_summary
        return product


    def record_inventory_fetch(
        self,
        watch_id: int,
        shop_refresh_cache: dict[int, dict[str, Any] | Exception] | None = None,
    ) -> dict[str, Any]:
        shop_id = self._watch_inventory_shop_id(watch_id)
        if shop_id is None:
            product = self.record_fetch(watch_id)
            product["refresh_source"] = "item"
            return product

        cached = shop_refresh_cache.get(shop_id) if shop_refresh_cache is not None else None
        if isinstance(cached, Exception):
            raise RuntimeError(str(cached)) from cached
        if isinstance(cached, dict):
            summary = cached
        else:
            try:
                summary = self.record_shop_fetch(shop_id)
            except Exception as exc:
                if shop_refresh_cache is not None:
                    shop_refresh_cache[shop_id] = exc
                raise
            if shop_refresh_cache is not None:
                shop_refresh_cache[shop_id] = summary
        return self._latest_watch_product(watch_id, shop_id=shop_id, shop_summary=summary)


    def list_shops(self) -> list[dict[str, Any]]:
        with self.database() as connection:
            result: list[dict[str, Any]] = []
            for row in connection.execute("SELECT * FROM shops ORDER BY id DESC"):
                latest_run = connection.execute(
                    "SELECT * FROM shop_runs WHERE shop_id = ? ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                product_rows = connection.execute(
                    "SELECT watch_id FROM shop_products WHERE shop_id = ? AND listed = 1",
                    (row["id"],),
                ).fetchall()
                known_stock_count = 0
                total_stock = 0
                on_sale_count = 0
                for product_row in product_rows:
                    snapshot = connection.execute(
                        """
                        SELECT stock, sale_status FROM snapshots
                        WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1
                        """,
                        (product_row["watch_id"],),
                    ).fetchone()
                    if snapshot is None:
                        continue
                    if snapshot["sale_status"] == "on_sale":
                        on_sale_count += 1
                    if str(snapshot["stock"] or "").isdigit():
                        known_stock_count += 1
                        total_stock += int(snapshot["stock"])
                shop = dict(row)
                shop["enabled"] = bool(shop["enabled"])
                shop["last_attempt"] = dict(latest_run) if latest_run else None
                shop["product_count"] = len(product_rows)
                shop["on_sale_count"] = on_sale_count
                shop["known_stock_count"] = known_stock_count
                shop["total_stock"] = total_stock if known_stock_count else None
                result.append(shop)
            return result


    def list_watches(self) -> list[dict[str, Any]]:
        with self.database() as connection:
            result: list[dict[str, Any]] = []
            for row in connection.execute("SELECT * FROM watches ORDER BY id DESC"):
                latest = connection.execute(
                    "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                attempt = connection.execute(
                    "SELECT fetched_at, status, error FROM snapshots WHERE watch_id = ? ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                previous = connection.execute(
                    "SELECT price FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1 OFFSET 1",
                    (row["id"],),
                ).fetchone()
                watch = dict(row)
                watch["enabled"] = bool(watch["enabled"])
                watch["latest"] = self.effective_watch_product(connection, row["id"], latest, attempt)
                watch["last_attempt"] = dict(attempt) if attempt else None
                shop_links = connection.execute(
                    """
                    SELECT s.id, s.name, s.token FROM shop_products sp
                    JOIN shops s ON s.id = sp.shop_id
                    WHERE sp.watch_id = ? AND sp.listed = 1
                    """,
                    (row["id"],),
                ).fetchall()
                watch["shops"] = [dict(link) for link in shop_links]
                watch["price_changed"] = bool(
                    latest and previous and latest["price"] not in (None, "") and latest["price"] != previous["price"]
                )
                result.append(watch)
            return result

    def delete_watches(self, watch_ids: list[int]) -> int:
        if not watch_ids:
            return 0
        placeholders = ",".join("?" for _ in watch_ids)
        with self.database() as connection:
            linked_products = connection.execute(
                f"""
                SELECT shop_id, goods_key FROM shop_products
                WHERE watch_id IN ({placeholders})
                """,
                watch_ids,
            ).fetchall()
            for link in linked_products:
                connection.execute(
                    """
                    INSERT INTO shop_exclusions(shop_id, goods_key, removed_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(shop_id, goods_key) DO UPDATE SET removed_at = excluded.removed_at
                    """,
                    (link["shop_id"], link["goods_key"], self.now()),
                )
            cursor = connection.execute(
                f"DELETE FROM watches WHERE id IN ({placeholders})",
                watch_ids,
            )
        return cursor.rowcount

    def delete_shops(self, shop_ids: list[int]) -> int:
        if not shop_ids:
            return 0
        placeholders = ",".join("?" for _ in shop_ids)
        with self.database() as connection:
            cursor = connection.execute(
                f"DELETE FROM shops WHERE id IN ({placeholders})",
                shop_ids,
            )
        return cursor.rowcount
