"""Price and inventory history queries for the monitoring radar."""

from __future__ import annotations

import math
from typing import Any, Callable


def price_history(
    database: Callable[[], Any],
    money: Callable[[Any], str],
    watch_id: int,
    *,
    limit: int,
    offset: int = 0,
    start_date: str = "",
    end_date: str = "",
    status: str = "all",
    stock: str = "all",
    query: str = "",
    missing_message: str = "Watch product does not exist",
) -> dict[str, Any]:
    """Return a paged history view plus compact price and stock statistics."""
    limit = min(max(int(limit or 25), 1), 200)
    offset = max(int(offset or 0), 0)
    status = status if status in {"all", "success", "error"} else "all"
    stock = stock if stock in {"all", "in", "out", "unknown"} else "all"
    start_date = str(start_date or "").strip()[:10]
    end_date = str(end_date or "").strip()[:10]
    query = str(query or "").strip()[:120]

    clauses = ["watch_id = ?"]
    params: list[Any] = [watch_id]
    if start_date:
        clauses.append("fetched_at >= ?")
        params.append(f"{start_date}T00:00:00")
    if end_date:
        clauses.append("fetched_at <= ?")
        params.append(f"{end_date}T23:59:59")
    if status != "all":
        clauses.append("status = ?")
        params.append(status)
    numeric_stock = "stock IS NOT NULL AND stock != '' AND stock NOT GLOB '*[^0-9]*'"
    if stock == "in":
        clauses.append(f"{numeric_stock} AND CAST(stock AS INTEGER) > 0")
    elif stock == "out":
        clauses.append(f"{numeric_stock} AND CAST(stock AS INTEGER) <= 0")
    elif stock == "unknown":
        clauses.append(f"NOT ({numeric_stock})")
    if query:
        clauses.append("(title LIKE ? OR price LIKE ? OR stock LIKE ? OR sale_status LIKE ? OR error LIKE ?)")
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern, pattern, pattern])

    where_sql = " AND ".join(clauses)
    with database() as connection:
        if not connection.execute("SELECT 1 FROM watches WHERE id = ?", (watch_id,)).fetchone():
            raise KeyError(missing_message)
        total = int(connection.execute(f"SELECT COUNT(*) FROM snapshots WHERE {where_sql}", params).fetchone()[0])
        stats_row = connection.execute(
            f"""
            SELECT
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
                SUM(CASE WHEN status = 'success' AND {numeric_stock} AND CAST(stock AS INTEGER) > 0 THEN 1 ELSE 0 END) AS in_stock_count,
                SUM(CASE WHEN status = 'success' AND {numeric_stock} AND CAST(stock AS INTEGER) <= 0 THEN 1 ELSE 0 END) AS out_stock_count,
                SUM(CASE WHEN status = 'success' AND NOT ({numeric_stock}) THEN 1 ELSE 0 END) AS unknown_stock_count
            FROM snapshots WHERE {where_sql}
            """,
            params,
        ).fetchone()
        rows = connection.execute(
            f"""SELECT id, title, price, stock, sale_status, fetched_at, status, error
            FROM snapshots WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        trend_rows = connection.execute(
            f"""SELECT id, price, stock, sale_status, fetched_at, status, error FROM (
                SELECT id, price, stock, sale_status, fetched_at, status, error
                FROM snapshots WHERE {where_sql} AND status = 'success'
                ORDER BY fetched_at DESC, id DESC LIMIT 720
            ) ORDER BY fetched_at ASC, id ASC""",
            params,
        ).fetchall()
        analysis_rows = connection.execute(
            f"""SELECT id, price, stock, fetched_at
            FROM snapshots WHERE {where_sql} AND status = 'success'
            ORDER BY fetched_at ASC, id ASC""",
            params,
        ).fetchall()

    def serialize_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        raw_stock = result.get("stock")
        result["stock"] = int(raw_stock) if str(raw_stock or "").isdigit() else None
        result["stock_label"] = str(raw_stock) if raw_stock not in (None, "") else "接口未公开数量"
        return result

    price_points: list[tuple[float, str]] = []
    stock_points: list[int] = []
    price_change_count = 0
    restock_count = 0
    sold_out_count = 0
    previous_price_value: float | None = None
    previous_stock_value: int | None = None
    for row in analysis_rows:
        try:
            price_value = float(row["price"])
        except (TypeError, ValueError):
            price_value = None
        if price_value is not None and not math.isfinite(price_value):
            price_value = None
        if price_value is not None:
            if previous_price_value is not None and price_value != previous_price_value:
                price_change_count += 1
            price_points.append((price_value, row["fetched_at"]))
            previous_price_value = price_value

        raw_stock = row["stock"]
        stock_value = int(raw_stock) if str(raw_stock or "").isdigit() else None
        if stock_value is not None:
            if previous_stock_value is not None:
                if previous_stock_value <= 0 < stock_value:
                    restock_count += 1
                elif previous_stock_value > 0 >= stock_value:
                    sold_out_count += 1
            stock_points.append(stock_value)
            previous_stock_value = stock_value

    prices = [point[0] for point in price_points]
    first_price = prices[0] if prices else None
    latest_price = prices[-1] if prices else None
    previous_price = prices[-2] if len(prices) > 1 else None
    price_change = latest_price - first_price if latest_price is not None and first_price is not None else None
    latest_change = latest_price - previous_price if latest_price is not None and previous_price is not None else None
    average_price = sum(prices) / len(prices) if prices else None
    variance = sum((value - average_price) ** 2 for value in prices) / len(prices) if prices else None
    volatility_percent = ((variance ** 0.5) / average_price * 100) if variance is not None and average_price else None
    numeric_stock_count = len(stock_points)
    in_stock_count = sum(1 for value in stock_points if value > 0)
    min_point = min(price_points, key=lambda point: point[0]) if price_points else None
    max_point = max(price_points, key=lambda point: point[0]) if price_points else None

    stats = {
        "success_count": int(stats_row["success_count"] or 0),
        "error_count": int(stats_row["error_count"] or 0),
        "quoted_count": len(prices),
        "min_price": money(min_point[0]) if min_point else None,
        "max_price": money(max_point[0]) if max_point else None,
        "average_price": money(average_price) if average_price is not None else None,
        "in_stock_count": int(stats_row["in_stock_count"] or 0),
        "out_stock_count": int(stats_row["out_stock_count"] or 0),
        "unknown_stock_count": int(stats_row["unknown_stock_count"] or 0),
        "first_price": money(first_price) if first_price is not None else None,
        "latest_price": money(latest_price) if latest_price is not None else None,
        "previous_price": money(previous_price) if previous_price is not None else None,
        "price_change": money(price_change) if price_change is not None else None,
        "price_change_percent": round(price_change / first_price * 100, 2) if price_change is not None and first_price else None,
        "latest_change": money(latest_change) if latest_change is not None else None,
        "latest_change_percent": round(latest_change / previous_price * 100, 2) if latest_change is not None and previous_price else None,
        "volatility_percent": round(volatility_percent, 2) if volatility_percent is not None else None,
        "price_change_count": price_change_count,
        "restock_count": restock_count,
        "sold_out_count": sold_out_count,
        "availability_rate": round(in_stock_count / numeric_stock_count * 100, 2) if numeric_stock_count else None,
        "first_at": price_points[0][1] if price_points else None,
        "latest_at": price_points[-1][1] if price_points else None,
        "min_price_at": min_point[1] if min_point else None,
        "max_price_at": max_point[1] if max_point else None,
    }
    return {
        "items": [serialize_row(row) for row in rows],
        "trend": [serialize_row(row) for row in trend_rows],
        "total": total,
        "page": offset // limit + 1,
        "page_size": limit,
        "stats": stats,
        "filters": {"start_date": start_date, "end_date": end_date, "status": status, "stock": stock, "query": query},
    }
