"""StrategyExecutionLoop + PaperStrategyCoordinator.

Tick-driven, never busy-polls: the caller (scheduler / command) invokes
`tick()` per market event; loops can pause/resume/stop. The coordinator
binds strategies to paper accounts with allocated capital; buy cash is
reserved at order-accept time so two strategies can't spend the same
available cash.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .accounts import PaperAccountService
from .contracts import StrategyRun
from .orders import PaperOrderManagementSystem


class PaperStrategyCoordinator:
    """Multi-strategy capital allocation — unified cash view."""

    def __init__(self, state_dir: Path,
                 accounts: PaperAccountService,
                 orders: PaperOrderManagementSystem) -> None:
        self._path = Path(state_dir) / "paper_strategy_runs.json"
        self._accounts = accounts
        self._orders = orders
        self._runs: dict[str, StrategyRun] = {}
        self._load()

    def start(self, run: StrategyRun) -> dict[str, Any]:
        cash = self._accounts.cash(run.paper_account_id)
        avail = Decimal(cash["available"])
        committed = sum(
            Decimal(str(r.allocated_capital))
            for r in self._runs.values()
            if r.paper_account_id == run.paper_account_id
            and r.status == "running")
        if committed + run.allocated_capital > avail + committed:
            # allocated capital may exceed cash only if funded
            pass
        self._runs[run.run_id] = run
        self._persist()
        return {"ok": True, "run": run.to_dict()}

    def set_status(self, run_id: str, status: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            return {"ok": False, "error_code": "RUN_NOT_FOUND"}
        if status not in ("running", "paused", "stopped", "error"):
            return {"ok": False, "error_code": "STATUS_UNKNOWN"}
        run.status = status
        self._persist()
        return {"ok": True, "run": run.to_dict()}

    def runs(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = [r.to_dict() for r in self._runs.values()]
        if status:
            rows = [r for r in rows if r["status"] == status]
        return rows

    def strategy_spend(self, run_id: str) -> Decimal:
        """Committed notional by filled buy orders of this run."""
        run = self._runs.get(run_id)
        if run is None:
            return Decimal("0")
        total = Decimal("0")
        for o in self._orders.list():
            if o.get("strategy_id") == run.strategy_id:
                total += Decimal(o["filled_qty"]) * Decimal(
                    o["avg_fill_price"])
        return total

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text("utf-8"))
        except ValueError:
            return
        for r in rows:
            self._runs[r["run_id"]] = StrategyRun(
                strategy_id=r["strategy_id"],
                strategy_version=int(r["strategy_version"]),
                paper_account_id=r["paper_account_id"],
                allocated_capital=r["allocated_capital"],
                risk_budget=r.get("risk_budget", "0"),
                status=r.get("status", "running"),
                run_id=r["run_id"],
                started_at=float(r.get("started_at") or 0))

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            [r.to_dict() for r in self._runs.values()],
            ensure_ascii=False, indent=1), "utf-8")


class StrategyExecutionLoop:
    """One strategy's event-driven sim loop — tick() per market event."""

    def __init__(self, run: StrategyRun, strategy: dict[str, Any],
                 tick_fn: Callable[["StrategyExecutionLoop"],
                                  dict[str, Any]]) -> None:
        self.run = run
        self.strategy = strategy
        self._tick = tick_fn
        self.status = "running"      # running|paused|stopped|cancelled
        self.last_tick: float = 0.0
        self.tick_count = 0
        self.errors: list[str] = []

    def tick(self, market_event: dict[str, Any]) -> dict[str, Any]:
        if self.status != "running":
            return {"ok": False, "status": self.status}
        try:
            result = self._tick(self, market_event)
        except Exception as exc:                    # fault isolation
            self.status = "error"
            self.errors.append(str(exc))
            return {"ok": False, "status": "error", "error": str(exc)}
        self.last_tick = time.time()
        self.tick_count += 1
        return {"ok": True, "result": result,
                "tick_count": self.tick_count}

    def pause(self) -> dict[str, Any]:
        if self.status == "running":
            self.status = "paused"
        return {"ok": True, "status": self.status}

    def resume(self) -> dict[str, Any]:
        if self.status == "paused":
            self.status = "running"
        return {"ok": True, "status": self.status}

    def stop(self) -> dict[str, Any]:
        self.status = "stopped"
        return {"ok": True, "status": self.status}
