"""Automated watch-product alerts (restock / price-drop) with SMTP email.

Design decisions:
- Zero intrusion: no changes to the fetch/inventory pipeline.  The alert
  scanner runs as its own daemon thread on a short timer and compares the two
  most recent successful snapshots for every *armed* watch.
- Extensible: alert kinds live in a small registry; adding a new kind later
  means adding one entry here plus the DB check.
- Suppression: a price-drop alert fires only when the newest price drops below
  the previously recorded trigger price (initially the snapshot before it), so
  the same price never spams the mailbox.
"""

from __future__ import annotations

import json
import smtplib
import threading
import time
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any, Callable

from .settings import read_json, store_json

SETTINGS_KEY = "alert_email"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "smtp_host": "",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "recipient": "",
    "use_tls": True,
}

# --------------------------------------------------------------------------
# Parsing helpers (snapshot rows -> plain values)
# --------------------------------------------------------------------------

def _money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return None


def _stock(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(str(value).strip().split(" ")[0])
        return max(0, parsed)
    except (TypeError, ValueError):
        return None


def _is_available(price: Any, stock: Any, sale_status: Any) -> bool:
    """True when the snapshot represents something purchasable right now."""
    if sale_status not in (None, "") and sale_status != "on_sale":
        return False
    stock_value = _stock(stock)
    if stock_value is not None:
        return stock_value > 0
    return _money(price) is not None


# --------------------------------------------------------------------------
# Alert service
# --------------------------------------------------------------------------

class AlertService:
    def __init__(
        self,
        *,
        database: Callable[[], Any],
        now: Callable[[], str],
        scan_interval_seconds: int = 60,
    ) -> None:
        self._database = database
        self._now = now
        self._interval = max(15, scan_interval_seconds)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run_loop, name="alert-scanner", daemon=True
            )
            thread.start()
            self._thread = thread

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _run_loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.scan_once()
            except Exception:
                # The scanner must never take the process down; the next tick
                # retries automatically.
                pass
            try:
                self.scan_categories()
            except Exception:
                pass

    # -- settings ----------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        return read_json(self._database, SETTINGS_KEY, dict(DEFAULT_SETTINGS))

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get_settings()
        for key, value in patch.items():
            if key in DEFAULT_SETTINGS:
                current[key] = value
        store_json(self._database, SETTINGS_KEY, current)
        return current

    # -- alert registry ----------------------------------------------------

    def _ensure_table(self, connection: Any) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_triggered_price TEXT,
                last_notified_at TEXT,
                created_at TEXT,
                UNIQUE(watch_id, kind)
            )
            """
        )

    def add(self, watch_id: int, kind: str) -> dict[str, Any]:
        kind = str(kind or "").strip()
        if kind not in ("restock", "price_drop"):
            raise ValueError("提醒类型无效")
        with self._database() as connection:
            self._ensure_table(connection)
            connection.execute(
                """
                INSERT INTO alerts(watch_id, kind, enabled, created_at)
                VALUES(?, ?, 1, ?)
                ON CONFLICT(watch_id, kind) DO UPDATE SET enabled = 1
                """,
                (int(watch_id), kind, self._now()),
            )
        return {"ok": True, "watch_id": int(watch_id), "kind": kind, "enabled": True}

    def remove(self, watch_id: int, kind: str) -> dict[str, Any]:
        with self._database() as connection:
            self._ensure_table(connection)
            connection.execute(
                "DELETE FROM alerts WHERE watch_id = ? AND kind = ?",
                (int(watch_id), str(kind or "").strip()),
            )
        return {"ok": True}

    def list(self) -> list[dict[str, Any]]:
        with self._database() as connection:
            self._ensure_table(connection)
            rows = connection.execute(
                """
                SELECT a.id, a.watch_id, a.kind, a.enabled, a.last_notified_at,
                       w.name AS watch_name, w.url AS watch_url,
                       (SELECT price FROM snapshots WHERE watch_id = a.watch_id
                         AND status = 'success' ORDER BY id DESC LIMIT 1) AS last_price,
                       (SELECT sale_status FROM snapshots WHERE watch_id = a.watch_id
                         AND status = 'success' ORDER BY id DESC LIMIT 1) AS last_sale_status
                FROM alerts a
                LEFT JOIN watches w ON w.id = a.watch_id
                ORDER BY a.id DESC
                """
            ).fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row["id"],
                "watch_id": row["watch_id"],
                "kind": row["kind"],
                "enabled": bool(row["enabled"]),
                "last_notified_at": row["last_notified_at"],
                "watch_name": row["watch_name"] or "",
                "watch_url": row["watch_url"] or "",
                "last_price": row["last_price"],
                "last_sale_status": row["last_sale_status"],
            })
        return result

    # ------------------------------------------------------------------
    # Category ("group new arrival") alerts: watch a stable shop category
    # for NEW products. Sellers re-list goods under fresh keys all the
    # time, but the category itself is stable, so "new key in category"
    # is the reliable signal the user cares about.
    # ------------------------------------------------------------------
    def _ensure_category_table(self, connection: Any) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS category_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_token TEXT NOT NULL,
                shop_name TEXT,
                category TEXT NOT NULL,
                seen_keys TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_notified_at TEXT,
                created_at TEXT,
                UNIQUE(shop_token, category)
            )
            """
        )

    def _category_live_keys(self, shop_token: str, category: str):
        """Return ([(goods_key, title)], {goods_key: url}) for live on-sale items."""
        with self._database() as connection:
            rows = connection.execute(
                """
                SELECT s.goods_key, s.title, s.raw_data, s.sale_status
                FROM snapshots s
                JOIN (
                    SELECT watch_id, MAX(id) AS mid FROM snapshots
                    WHERE status = 'success' GROUP BY watch_id
                ) m ON m.mid = s.id
                WHERE s.sale_status = 'on_sale'
                  AND s.goods_key IS NOT NULL AND s.goods_key != ''
                """
            ).fetchall()
        live = []
        urls = {}
        for row in rows:
            raw = {}
            if row["raw_data"]:
                try:
                    raw = json.loads(row["raw_data"])
                except Exception:
                    raw = {}
            if not isinstance(raw, dict):
                continue
            user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
            cat = raw.get("category") if isinstance(raw.get("category"), dict) else {}
            if str(user.get("token") or "") != shop_token:
                continue
            if str(cat.get("name") or "").strip() != category:
                continue
            key = str(row["goods_key"]).strip()
            title = str(row["title"] or "").strip()
            link = str(raw.get("link") or "https://wzyp.cn/item/" + key)
            live.append((key, title))
            urls[key] = link
        return live, urls

    def category_options(self):
        """Aggregate shops + live categories for the picker UI."""
        with self._database() as connection:
            rows = connection.execute(
                """
                SELECT s.goods_key, s.raw_data
                FROM snapshots s
                JOIN (
                    SELECT watch_id, MAX(id) AS mid FROM snapshots
                    WHERE status = 'success' GROUP BY watch_id
                ) m ON m.mid = s.id
                WHERE s.sale_status = 'on_sale' AND s.raw_data IS NOT NULL
                """
            ).fetchall()
        shops = {}
        for row in rows:
            raw = {}
            try:
                raw = json.loads(row["raw_data"])
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
            cat = raw.get("category") if isinstance(raw.get("category"), dict) else {}
            token = str(user.get("token") or "").strip()
            category = str(cat.get("name") or "").strip()
            if not token or not category:
                continue
            shop = shops.setdefault(token, {
                "shop_token": token,
                "shop_name": str(user.get("nickname") or token)[:100],
                "categories": {},
            })
            entry = shop["categories"].setdefault(category, {"category": category, "live_count": 0})
            entry["live_count"] += 1
        result = []
        for shop in shops.values():
            shop["categories"] = sorted(shop["categories"].values(), key=lambda c: c["category"].lower())
            result.append(shop)
        return sorted(result, key=lambda s: str(s["shop_name"]).lower())

    def add_category_alert(self, shop_token, category, shop_name=""):
        shop_token = str(shop_token or "").strip()
        category = str(category or "").strip()
        if not shop_token or not category or len(shop_token) > 80 or len(category) > 120:
            raise ValueError("店铺与分组不能为空且长度需合法")
        live, _urls = self._category_live_keys(shop_token, category)
        seen = json.dumps([key for key, _title in live], ensure_ascii=False)
        with self._database() as connection:
            self._ensure_category_table(connection)
            connection.execute(
                """
                INSERT INTO category_alerts(shop_token, shop_name, category, seen_keys, enabled, created_at)
                VALUES(?, ?, ?, ?, 1, ?)
                ON CONFLICT(shop_token, category) DO UPDATE SET
                    enabled = 1,
                    seen_keys = excluded.seen_keys
                """,
                (shop_token, str(shop_name or "").strip()[:100] or None, category, seen, self._now()),
            )
        return {"ok": True, "shop_token": shop_token, "category": category, "initial_items": len(live)}

    def remove_category_alert(self, shop_token, category):
        with self._database() as connection:
            self._ensure_category_table(connection)
            connection.execute(
                "DELETE FROM category_alerts WHERE shop_token = ? AND category = ?",
                (str(shop_token or "").strip(), str(category or "").strip()),
            )
        return {"ok": True}

    def list_category_alerts(self):
        with self._database() as connection:
            self._ensure_category_table(connection)
            rows = connection.execute("SELECT * FROM category_alerts ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            try:
                seen = json.loads(row["seen_keys"] or "[]")
            except Exception:
                seen = []
            result.append({
                "id": row["id"],
                "shop_token": row["shop_token"],
                "shop_name": row["shop_name"] or row["shop_token"],
                "category": row["category"],
                "enabled": bool(row["enabled"]),
                "watched_count": len(seen) if isinstance(seen, list) else 0,
                "last_notified_at": row["last_notified_at"],
            })
        return result

    def scan_categories(self):
        """For every armed category alert compare live keys with seen keys and
        email the newly appeared products."""
        with self._database() as connection:
            self._ensure_category_table(connection)
            armed = connection.execute("SELECT * FROM category_alerts WHERE enabled = 1").fetchall()
        fired = []
        for alert in armed:
            try:
                shop_token = str(alert["shop_token"])
                category = str(alert["category"])
                live, urls = self._category_live_keys(shop_token, category)
                if not live:
                    # Empty directory this tick: likely a WAF hiccup or the shop
                    # is fully down. Keep the old seen list to avoid a false
                    # "everything is new" notification later.
                    continue
                try:
                    seen = set(json.loads(alert["seen_keys"] or "[]"))
                except Exception:
                    seen = set()
                current_keys = {key for key, _title in live}
                fresh = [(key, title) for key, title in live if key not in seen]
                if not fresh:
                    continue
                merged = seen | current_keys
                trimmed = sorted(merged)[-800:]
                shop_name = str(alert["shop_name"] or shop_token)
                with self._database() as connection:
                    self._ensure_category_table(connection)
                    connection.execute(
                        "UPDATE category_alerts SET seen_keys = ?, last_notified_at = ? WHERE shop_token = ? AND category = ?",
                        (json.dumps(trimmed, ensure_ascii=False), self._now(), shop_token, category),
                    )
                delivered = self._send_email(
                    f"{shop_name} · {category}",
                    "分组上新",
                    "\n".join(f"- {title or key}  {urls.get(key, '')}" for key, title in fresh[:20]),
                    None,
                )
                fired.append({
                    "shop_token": shop_token,
                    "category": category,
                    "new_items": len(fresh),
                    "delivered": delivered,
                })
            except Exception:
                continue
        return {"scanned": len(armed), "fired": fired}

    # -- scanning / detection -----------------------------------------------

    def scan_once(self) -> dict[str, Any]:
        """Scan armed alerts; returns a summary for diagnostics."""
        with self._database() as connection:
            self._ensure_table(connection)
            armed = connection.execute(
                "SELECT * FROM alerts WHERE enabled = 1"
            ).fetchall()
        fired: list[dict[str, Any]] = []
        for alert in armed:
            try:
                fired_alert = self._check_alert(alert)
                if fired_alert:
                    fired.append(fired_alert)
            except Exception:
                # Never let one malformed alert block the others.
                continue
        return {"scanned": len(armed), "fired": fired}

    def _check_alert(self, alert: Any) -> dict[str, Any] | None:
        watch_id = int(alert["watch_id"])
        kind = str(alert["kind"])
        with self._database() as connection:
            rows = connection.execute(
                """
                SELECT id, price, stock, sale_status, fetched_at
                FROM snapshots
                WHERE watch_id = ? AND status = 'success'
                ORDER BY id DESC LIMIT 2
                """,
                (watch_id,),
            ).fetchall()
        if len(rows) < 2:
            # Need at least one previous baseline to decide "changed".
            return None
        current = rows[0]
        previous = rows[1]

        if kind == "restock":
            was_available = _is_available(previous["price"], previous["stock"], previous["sale_status"])
            now_available = _is_available(current["price"], current["stock"], current["sale_status"])
            if was_available or not now_available:
                return None
            reason = "补货"
            detail = f"从缺货变为有货（库存 {current['stock'] or '未知'}）"
        elif kind == "price_drop":
            now_price = _money(current["price"])
            previous_price = _money(previous["price"])
            if now_price is None or previous_price is None:
                return None
            baseline = _money(alert["last_triggered_price"])
            if baseline is not None:
                # Suppress unless the price fell even further.
                if now_price >= baseline:
                    return None
            elif now_price >= previous_price:
                # No prior baseline: trigger only on an actual drop vs previous.
                return None
            reason = "降价"
            detail = f"价格从 {previous['price']} 降至 {current['price']}"
        else:
            return None

        # Persist the trigger marker before emailing so a send failure still
        # suppresses a repeat for the same condition on the next tick.
        with self._database() as connection:
            self._ensure_table(connection)
            connection.execute(
                """
                UPDATE alerts
                SET last_triggered_price = ?, last_notified_at = ?
                WHERE watch_id = ? AND kind = ?
                """,
                (current["price"] if kind == "price_drop" else None, self._now(), watch_id, kind),
            )
            name_row = connection.execute(
                "SELECT name FROM watches WHERE id = ?", (watch_id,)
            ).fetchone()
            title = str(name_row["name"] if name_row else "").strip()

        delivered = self._send_email(title, reason, detail, watch_id)
        return {
            "watch_id": watch_id,
            "kind": kind,
            "title": title,
            "delivered": delivered,
        }

    # -- email ---------------------------------------------------------------

    def _send_email(self, title: str, reason: str, detail: str, watch_id: int) -> bool:
        settings = self.get_settings()
        if not settings.get("enabled"):
            return False
        recipient = str(settings.get("recipient") or "").strip()
        smtp_user = str(settings.get("smtp_user") or "").strip()
        smtp_password = str(settings.get("smtp_password") or "").strip()
        if not (recipient and smtp_user):
            return False
        host = str(settings.get("smtp_host") or "").strip()
        port = int(settings.get("smtp_port") or 465)
        sender = str(settings.get("smtp_from") or smtp_user).strip()
        subject = f"[链动监控] {title[:60]} - {reason}提醒"
        body = (
            f"你关注的商品有变化：\n\n"
            f"商品：{title}\n"
            f"提醒：{reason}\n"
            f"详情：{detail}\n"
            f"监控ID：{watch_id}\n\n"
            f"—— 链动本地监控台自动通知\n"
        )
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = recipient
        message["Date"] = formatdate(localtime=True)

        try:
            if bool(settings.get("use_tls", True)):
                server = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                server = smtplib.SMTP(host, port, timeout=20)
                server.starttls()
            try:
                server.login(smtp_user, smtp_password)
                server.sendmail(sender, [recipient], message.as_string())
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            return True
        except Exception as exc:
            # 抛出具体原因, 由上层 (do_POST) 捕获后写入响应 detail, 前端能看到真实错
            raise RuntimeError(f"SMTP 发送失败: {type(exc).__name__}: {exc}") from exc

    def send_test(self) -> dict[str, Any]:
        """Send a test email with the saved settings."""
        delivered = self._send_email("测试邮件", "测试", "这是一封测试通知邮件，收到说明邮箱配置成功。", 0)
        return {"delivered": delivered}
