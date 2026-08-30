"""Background worker for scheduled Sub2API reclaim cycles."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable


class Sub2ApiAutomationWorker(threading.Thread):
    def __init__(
        self,
        *,
        run_cycle: Callable[[], dict[str, Any]],
        settings_loader: Callable[[], dict[str, Any]],
        state_loader: Callable[[], dict[str, Any]],
        store_state: Callable[[dict[str, Any]], None],
        now: Callable[[], str],
    ) -> None:
        super().__init__(name="sub2api-401-automation", daemon=True)
        self._run_cycle = run_cycle
        self._settings_loader = settings_loader
        self._state_loader = state_loader
        self._store_state = store_state
        self._now = now
        self.stop_event = threading.Event()
        self.run_lock = threading.Lock()

    def run_once(self) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            return {"ok": True, "skipped": True, "reason": "busy"}
        try:
            return self._run_cycle()
        except Exception as exc:
            previous = self._state_loader()
            state = {
                "last_run": self._now(),
                "last_error": str(exc)[:500],
                "last_result": previous.get("last_result"),
                "pending_card_codes": previous.get("pending_card_codes", []),
                "imported_order_nos": previous.get("imported_order_nos", []),
                "run_history": (
                    previous.get("run_history", [])
                    + [{"run_at": self._now(), "status": "error", "error": str(exc)[:500]}]
                )[-20:],
            }
            self._store_state(state)
            return {"ok": False, "detail": state["last_error"], "state": state}
        finally:
            self.run_lock.release()

    def run(self) -> None:
        while not self.stop_event.wait(1):
            settings = self._settings_loader()
            if not settings["enabled"]:
                continue
            state = self._state_loader()
            last_run = 0.0
            if state.get("last_run"):
                try:
                    last_run = datetime.fromisoformat(str(state["last_run"])).timestamp()
                except ValueError:
                    pass
            interval = (
                min(settings["interval_seconds"], 10)
                if state["pending_card_codes"]
                else settings["interval_seconds"]
            )
            if time.time() - last_run >= interval:
                self.run_once()
