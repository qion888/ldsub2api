"""Preorder validation, persistence, and order-trigger lifecycle."""

from __future__ import annotations

from typing import Any, Callable


class PreorderConflict(RuntimeError):
    pass


def list_preorders(
    *,
    database: Callable[[], Any],
    effective_product: Callable[..., dict[str, Any] | None],
) -> list[dict[str, Any]]:
    with database() as connection:
        rows = connection.execute("SELECT * FROM preorders ORDER BY id DESC").fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            watch = connection.execute(
                "SELECT url, name FROM watches WHERE id = ?", (row["watch_id"],)
            ).fetchone()
            latest = connection.execute(
                "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                (row["watch_id"],),
            ).fetchone()
            product = effective_product(connection, row["watch_id"], latest)
            item = dict(row)
            item.pop("contact", None)
            item.pop("query_password", None)
            item["enabled"] = bool(item["enabled"])
            item["title"] = (product or {}).get("title") or (watch["name"] if watch else "") or f"商品 {row['watch_id']}"
            item["official_url"] = watch["url"] if watch else ""
            item["current_stock"] = (product or {}).get("stock")
            item["stock_label"] = (product or {}).get("stock_label") or "数量待获取"
            item["sale_status"] = (product or {}).get("sale_status")
            result.append(item)
    return result


def create_preorders(
    payload: dict[str, Any],
    *,
    database: Callable[[], Any],
    settings_loader: Callable[[], dict[str, Any]],
    interval_normalizer: Callable[[Any, int], int],
    now: Callable[[], str],
    effective_product: Callable[..., dict[str, Any] | None],
    list_loader: Callable[[], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if payload.get("enabled") is not True:
        raise ValueError("请先勾选启用自动预购")
    entries = payload.get("items")
    if not isinstance(entries, list) or not entries or len(entries) > 100:
        raise ValueError("预购清单应包含 1 到 100 个商品")
    config = settings_loader()
    try:
        interval = interval_normalizer(payload.get("interval_seconds"), 1)
        channel_id = int(config.get("channel_id", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("预购配置无效") from exc
    contact = str(config.get("contact") or "").strip()
    query_password = str(config.get("query_password") or "")
    if not contact:
        raise PreorderConflict("请先保存购买联系方式")
    if channel_id < 1 or channel_id > 99:
        raise ValueError("支付渠道配置无效")

    normalized: list[tuple[int, int]] = []
    seen: set[int] = set()
    for entry in entries:
        try:
            watch_id = int(entry.get("watch_id"))
            quantity = int(entry.get("quantity", 1))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("预购商品或数量格式无效") from exc
        if watch_id in seen:
            raise ValueError("预购清单包含重复商品")
        if quantity < 1 or quantity > 99:
            raise ValueError("单项预购数量应为 1 到 99")
        seen.add(watch_id)
        normalized.append((watch_id, quantity))

    stamp = now()
    with database() as connection:
        validated: list[tuple[int, int]] = []
        for watch_id, quantity in normalized:
            watch = connection.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
            latest = connection.execute(
                "SELECT * FROM snapshots WHERE watch_id = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
                (watch_id,),
            ).fetchone()
            if watch is None or latest is None:
                raise PreorderConflict(f"商品 {watch_id} 尚无有效库存数据")
            product = effective_product(connection, watch_id, latest)
            title = product.get("title") or watch["name"] or f"商品 {watch_id}"
            if product.get("sale_status") != "on_sale":
                raise PreorderConflict(f"{title} 当前未上架，不能启用预购")
            if product.get("stock") is None:
                raise PreorderConflict(f"{title} 的库存数量未知，不能启用预购")
            if product["stock"] != 0:
                raise PreorderConflict(f"{title} 当前不是缺货状态，不会启用预购")
            minimum = int(product.get("limit_count") or 1)
            if quantity < minimum:
                raise PreorderConflict(f"{title} 最低 {minimum} 件起购")
            if product.get("query_password_required") and not query_password:
                raise PreorderConflict(f"{title} 需要查询密码，请先保存购买配置")
            shop = connection.execute(
                """
                SELECT s.token FROM shops s JOIN shop_products sp ON sp.shop_id = s.id
                WHERE sp.watch_id = ? AND sp.listed = 1 ORDER BY s.id LIMIT 1
                """,
                (watch_id,),
            ).fetchone()
            if shop is None:
                raise PreorderConflict(f"{title} 未关联店铺，请先通过店铺链接同步")
            processing = connection.execute(
                "SELECT 1 FROM preorders WHERE watch_id = ? AND status = 'processing'",
                (watch_id,),
            ).fetchone()
            if processing:
                raise PreorderConflict(f"{title} 正在创建订单，请勿重复设置")
            validated.append((watch_id, quantity))

        for watch_id, quantity in validated:
            connection.execute(
                """
                INSERT INTO preorders(
                    watch_id, quantity, interval_seconds, enabled, status, contact,
                    query_password, channel_id, last_check, last_error, trade_no,
                    payment_url, amount, created_at, triggered_at
                ) VALUES(?, ?, ?, 1, 'watching', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, NULL)
                ON CONFLICT(watch_id) DO UPDATE SET
                    quantity = excluded.quantity,
                    interval_seconds = excluded.interval_seconds,
                    enabled = 1,
                    status = 'watching',
                    contact = excluded.contact,
                    query_password = excluded.query_password,
                    channel_id = excluded.channel_id,
                    last_check = NULL,
                    last_error = NULL,
                    trade_no = NULL,
                    payment_url = NULL,
                    amount = NULL,
                    created_at = excluded.created_at,
                    triggered_at = NULL
                """,
                (watch_id, quantity, interval, contact, query_password, channel_id, stamp),
            )
    return list_loader()


def process_preorder(
    preorder_id: int,
    product: dict[str, Any],
    *,
    database: Callable[[], Any],
    now: Callable[[], str],
    identity_loader: Callable[[str], dict[str, Any]],
    order_creator: Callable[..., dict[str, Any]],
) -> dict[str, Any] | None:
    stamp = now()
    with database() as connection:
        preorder = connection.execute(
            "SELECT * FROM preorders WHERE id = ? AND enabled = 1 AND status = 'watching'",
            (preorder_id,),
        ).fetchone()
        if preorder is None:
            return None
        connection.execute(
            "UPDATE preorders SET last_check = ?, last_error = NULL WHERE id = ?",
            (stamp, preorder_id),
        )
        stock = product.get("stock")
        if product.get("sale_status") != "on_sale" or stock is None or int(stock) < int(preorder["quantity"]):
            return None
        claimed = connection.execute(
            "UPDATE preorders SET status = 'processing' WHERE id = ? AND status = 'watching' AND enabled = 1",
            (preorder_id,),
        )
        if claimed.rowcount != 1:
            return None
        watch = connection.execute("SELECT url FROM watches WHERE id = ?", (preorder["watch_id"],)).fetchone()
        shop = connection.execute(
            """
            SELECT s.token FROM shops s JOIN shop_products sp ON sp.shop_id = s.id
            WHERE sp.watch_id = ? AND sp.listed = 1 ORDER BY s.id LIMIT 1
            """,
            (preorder["watch_id"],),
        ).fetchone()

    try:
        if watch is None or shop is None:
            raise RuntimeError("商品店铺关联已失效")
        goods_key = str(product.get("goods_key") or "").strip()
        if not goods_key:
            raise RuntimeError("商品编号不可用")
        identity = identity_loader(shop["token"])
        order = order_creator(
            goods_key=goods_key,
            quantity=int(preorder["quantity"]),
            coupon_code="",
            channel_id=int(preorder["channel_id"]),
            contact=preorder["contact"],
            query_password=preorder["query_password"] if product.get("query_password_required") else "",
            select_cards_ids=[],
            juuid=identity["juuid"],
            referer=watch["url"],
        )
    except Exception as exc:
        with database() as connection:
            connection.execute(
                "UPDATE preorders SET enabled = 0, status = 'error', last_error = ? WHERE id = ? AND status = 'processing'",
                (str(exc)[:240] or "创建订单失败", preorder_id),
            )
        return {"status": "error", "detail": str(exc)}

    with database() as connection:
        connection.execute(
            """
            UPDATE preorders SET enabled = 0, status = 'triggered', trade_no = ?,
                payment_url = ?, amount = ?, triggered_at = ?, last_error = NULL
            WHERE id = ? AND status = 'processing'
            """,
            (order.get("trade_no"), order.get("payment_url"), order.get("amount"), now(), preorder_id),
        )
    return {"status": "triggered", "order": order}


def mark_check_error(
    preorder_id: int,
    error: Exception,
    *,
    database: Callable[[], Any],
    now: Callable[[], str],
) -> None:
    with database() as connection:
        connection.execute(
            "UPDATE preorders SET last_check = ?, last_error = ? WHERE id = ? AND status = 'watching'",
            (now(), str(error)[:240] or "库存检查失败", preorder_id),
        )
