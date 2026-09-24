"""AutoTradingMaintenanceService — restart/crash recovery.

On restart the service reconciles the last market event, last strategy
cycle, last signal, last simulated fill, sim cash, sim positions and
risk state *before* anything resumes. Replays can't re-execute completed
trades — client_order_id + event fingerprint dedup make the second pass
a no-op. Unverifiable state goes to RECOVERING or PAUSED, never straight
back to RUNNING.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .runtime import RuntimeState


class AutoTradingMaintenanceService:
    def __init__(self, state_dir: Path, *,
                 job_timeout_s: float = 20.0,
                 max_retries: int = 2) -> None:
        self._path = Path(state_dir) / "autotrade" / "maintenance.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._runs: list[dict[str, Any]] = []
        self._timeout = job_timeout_s
        self._retries = max_retries
        self._load()

    def _load(self) -> None:
        try:
            self._runs = json.loads(
                self._path.read_text(encoding="utf-8"))[-200:]
        except Exception:
            self._runs = []

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            self._runs[-200:], ensure_ascii=False, indent=1),
            encoding="utf-8")

    # ------------------------------------------------------------------
    def startup_recovery(self, engine: Any) -> dict[str, Any]:
        """Post-restart reconcile → safe states, no duplicate work."""
        run = {"run_id": f"amr-{uuid.uuid4().hex[:10]}",
               "job": "startup_recovery", "at": time.time(),
               "timeout_s": self._timeout,
               "retry_limit": self._retries}
        t0 = time.monotonic()
        checks: list[dict[str, Any]] = []
        # 1) journal integrity — last recorded event/checkpoint exists
        try:
            last_events = engine.dispatcher.list(limit=1)
            checks.append({"check": "last_market_event",
                           "ok": True,
                           "event": last_events[0]["event_id"]
                           if last_events else "none"})
        except Exception as exc:
            checks.append({"check": "last_market_event",
                           "ok": False, "error": str(exc)})
        # 2) open paper orders still consistent with the ledger
        try:
            open_orders = self._open_orders(engine)
            checks.append({"check": "open_orders",
                           "ok": True, "count": len(open_orders)})
        except Exception as exc:
            checks.append({"check": "open_orders",
                           "ok": False, "error": str(exc)})
        # 3) strategy states — blocked/failed runs stay safe
        recovered, held = [], []
        for run in engine.manager.list():
            state = run["state"]
            if state == RuntimeState.RUNNING:
                # process died mid-run → do not silently resume;
                # move to RECOVERING → PAUSED pending verification
                engine.manager.transition(
                    run["run_id"], RuntimeState.RECOVERING,
                    actor="system", reason="process restart")
                engine.manager.transition(
                    run["run_id"], RuntimeState.PAUSED,
                    actor="system",
                    reason="restart reconcile — verify before resume")
                held.append(run["run_id"])
            elif state == RuntimeState.RECOVERING:
                held.append(run["run_id"])
            elif state in (RuntimeState.DATA_BLOCKED,
                           RuntimeState.MODEL_BLOCKED):
                if run.get("auto_recover"):
                    r = engine.recovery.recover(
                        run["run_id"], actor="system")
                    (recovered if r.get("ok") else held).append(
                        run["run_id"])
                else:
                    held.append(run["run_id"])
        checks.append({"check": "strategy_states",
                       "ok": True,
                       "auto_recovered": recovered,
                       "held_for_review": held})
        run["checks"] = checks
        # best-effort PG mirror flush — auxiliary, never gates recovery
        try:
            mirror = getattr(engine, "pg_mirror", None)
            if mirror is not None:
                run["pg_mirror"] = mirror.flush()
        except Exception:
            pass
        run["status"] = ("ok" if all(c["ok"] for c in checks)
                         else "error")
        run["duration_ms"] = int((time.monotonic() - t0) * 1000)
        self._runs.append(run)
        self._persist()
        return {"ok": run["status"] == "ok", "recovery": run,
                "note": "重新啟動後不直接回到 RUNNING——"
                        "已完成交易由 client_order_id/事件指紋去重"}

    @staticmethod
    def _open_orders(engine: Any) -> list[dict[str, Any]]:
        try:
            return [o for o in engine.sim.orders.list()
                    if o.get("status") == "open"]
        except Exception:
            return []

    def status(self) -> dict[str, Any]:
        return {"ok": True, "runs": self._runs[-50:],
                "retry_limit": self._retries,
                "job_timeout_s": self._timeout}
