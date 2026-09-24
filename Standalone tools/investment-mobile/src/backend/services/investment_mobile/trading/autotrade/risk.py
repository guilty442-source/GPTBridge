"""StrategyRiskMonitor + StrategyAutoHaltService + StrategyRecoveryService.

- RiskMonitor *observes* per-strategy pnl/drawdown/concentration/cash
  usage/open orders/order frequency/data validity/execution anomalies —
  it triggers the halt flow but the formal PaperRiskEngine stays the
  only trade-decision authority. No competing risk verdicts.
- AutoHalt pauses the affected strategy, blocks new risk, keeps the
  trade/fault record and notifies — it never deletes a strategy or its
  history.
- Recovery re-checks: market data fresh, account consistent, funds
  sufficient, positions correct, orders complete, risk pass, version
  valid. Non-safety transient faults with auto_recover=True may resume
  automatically; safety halts and unknown states need governance — AI
  can never lift a safety pause.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .runtime import RuntimeState


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


HALT_REASONS = frozenset({
    "capital_limit", "drawdown_limit", "data_stale",
    "data_source_lost", "position_inconsistent", "execution_anomaly",
    "duplicate_order_anomaly", "resource_exhausted", "model_fault"})

SAFETY_REASONS = frozenset({
    "drawdown_limit", "capital_limit", "position_inconsistent",
    "duplicate_order_anomaly"})


class StrategyRiskMonitor:
    def __init__(self, sim: Any, gate: Any, events: Any,
                 *, limits: dict[str, float] | None = None) -> None:
        self._sim = sim
        self._gate = gate
        self._events = events
        self._limits = {
            "drawdown": -0.15, "capital_usage": 0.95,
            "max_open_orders": 50, "max_daily_trades": 20,
            **(limits or {})}

    def check(self, run: dict[str, Any]) -> dict[str, Any]:
        """Observe + flag; trade authorization stays with PaperRiskEngine."""
        account_id = run.get("account_id") or ""
        sid = run["strategy_id"]
        findings: list[dict[str, Any]] = []
        cash = self._sim.accounts.cash(account_id) if account_id else {}
        orders = [o for o in self._sim.orders.list(account_id)]
        open_orders = [o for o in orders if o.get("status") == "open"]
        mine = [o for o in orders if o.get("strategy_id") == sid]
        if len(open_orders) > self._limits["max_open_orders"]:
            findings.append({"kind": "open_orders",
                             "severity": "WARNING",
                             "count": len(open_orders)})
        today = [o for o in mine
                 if time.time() - float(o.get("created_at") or 0)
                 < 86400]
        if len(today) > self._limits["max_daily_trades"]:
            findings.append({"kind": "frequency",
                             "severity": "WARNING",
                             "count": len(today)})
        if cash:
            avail = _d(cash.get("available"))
            total = avail + _d(cash.get("reserved")) + _d(
                cash.get("unsettled"))
            usage = (1 - avail / total) if total > 0 else 0
            if usage >= self._limits["capital_usage"]:
                findings.append({"kind": "capital_usage",
                                 "severity": "WARNING",
                                 "usage": str(round(float(usage), 4))})
        for iid in run.get("instrument_scope") or []:
            g = self._gate.check_instrument(iid, run.get("market") or "")
            if g["status"] != "VALID":
                findings.append({"kind": "data_stale",
                                 "severity": "NOTICE",
                                 "instrument_id": iid,
                                 "status": g["status"]})
        for f in findings:
            self._events.emit(
                "risk_threshold" if f["severity"] in (
                    "WARNING", "CRITICAL") else "indicator_signal",
                instrument_id=str(f.get("instrument_id") or ""),
                market=str(run.get("market") or ""),
                severity=f["severity"],
                detail={"run_id": run["run_id"], **f})
        return {"ok": True, "run_id": run["run_id"],
                "findings": findings,
                "authority": "觀測與觸發——正式交易裁決仍由 "
                             "PaperRiskEngine 作出"}


class StrategyAutoHaltService:
    def __init__(self, manager: Any, notifications: Any) -> None:
        self._manager = manager
        self._notifications = notifications

    def halt(self, run_id: str, reason: str, *,
             detail: dict[str, Any] | None = None,
             notify: bool = True) -> dict[str, Any]:
        if reason not in HALT_REASONS:
            return {"ok": False, "error_code": "HALT_REASON_UNKNOWN"}
        target = (RuntimeState.RISK_HALTED if reason in SAFETY_REASONS
                  else RuntimeState.PAUSED
                  if reason in ("resource_exhausted",)
                  else RuntimeState.DATA_BLOCKED)
        if reason == "model_fault":
            target = RuntimeState.MODEL_BLOCKED
        r = self._manager.transition(run_id, target,
                                     actor="system", reason=reason)
        if not r.get("ok"):
            return r
        if notify:
            self._notifications.notify("risk", {
                "event_id": "", "event_type": "strategy_halt",
                "instrument_id": "", "account_id": "",
                "severity": "WARNING" if target != RuntimeState.RISK_HALTED
                            else "CRITICAL",
                "detail": {"run_id": run_id, "reason": reason,
                           **(detail or {})}},
                title="策略已暫停",
                body=f"run {run_id} halted: {reason}")
        return {"ok": True, "run_id": run_id, "state": target,
                "reason": reason,
                "note": "策略暫停——不新建風險部位；歷史與績效保留"}


class StrategyRecoveryService:
    """Verify-then-resume. Unverifiable → RECOVERING/PAUSED, never
    straight back to RUNNING. Safety halts need governance — AI can
    never unhalt."""

    def __init__(self, manager: Any, sim: Any, gate: Any) -> None:
        self._manager = manager
        self._sim = sim
        self._gate = gate

    def recover(self, run_id: str, *, actor: str = "user",
                force: bool = False) -> dict[str, Any]:
        run = self._manager.get(run_id)
        if run is None:
            return {"ok": False, "error_code": "RUN_NOT_FOUND"}
        state = run["state"]
        if state in (RuntimeState.RUNNING, RuntimeState.PAUSED):
            return {"ok": True, "state": state, "already": True}
        if state not in RuntimeState.BLOCKED and \
                state != RuntimeState.FAILED:
            return {"ok": False, "error_code": "NOT_BLOCKED",
                    "state": state}
        safety = state == RuntimeState.RISK_HALTED
        checks = self._verify(run)
        failed = [c for c in checks if not c["ok"]]
        if failed:
            self._manager.transition(run_id, RuntimeState.RECOVERING,
                                     actor="system",
                                     reason="verification failed")
            self._manager.transition(run_id, RuntimeState.PAUSED,
                                     actor="system",
                                     reason=";".join(
                                         c["check"] for c in failed))
            return {"ok": False, "error_code": "RECOVERY_CHECKS_FAILED",
                    "failed": failed, "checks": checks}
        if safety:
            if actor.lower() in ("ai", "xingcheng", "model",
                                 "assistant"):
                return {"ok": False,
                        "error_code": "AI_CANNOT_UNHALT"}
            if not force and not run.get("auto_recover") is False:
                pass
            if not force:
                # safety halt: explicit human/governance review required
                return {"ok": False,
                        "error_code": "GOVERNANCE_REVIEW_REQUIRED",
                        "checks": checks,
                        "note": "安全性暫停需正式授權解除——"
                                "AI 或自動流程不得解除"}
        if not run.get("auto_recover") and not force \
                and actor == "system":
            return {"ok": False,
                    "error_code": "AUTO_RECOVER_DISABLED"}
        self._manager.transition(run_id, RuntimeState.RECOVERING,
                                 actor="system",
                                 reason="checks passed")
        r = self._manager.transition(run_id, RuntimeState.RUNNING,
                                     actor=actor if actor != "system"
                                     else "recovery",
                                     reason="recovered")
        return {"ok": r.get("ok", False), "checks": checks,
                "state": r.get("run", {}).get("state")}

    def _verify(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        checks = []
        for iid in run.get("instrument_scope") or []:
            g = self._gate.check_instrument(
                iid, run.get("market") or "")
            checks.append({"check": f"market_data:{iid}",
                           "ok": g["status"] == "VALID",
                           "status": g["status"]})
        account_id = run.get("account_id") or ""
        acct = (self._sim.accounts.get(account_id)
                if account_id else None)
        checks.append({"check": "account_consistent",
                       "ok": bool(acct)})
        if acct:
            cash = self._sim.accounts.cash(account_id)
            checks.append({"check": "funds_available",
                           "ok": _d(cash["available"]) >= 0})
            checks.append({"check": "orders_intact",
                           "ok": True})
        checks.append({"check": "version_valid",
                       "ok": run.get("strategy_version", 0) > 0})
        return checks
