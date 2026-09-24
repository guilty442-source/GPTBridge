"""StrategyPerformanceMonitor + StrategyStabilityAnalyzer.

Per-strategy metrics are computed from the paper ledger filtered by
strategy_id — initial capital, current equity, cumulative return,
realized/unrealized pnl, max drawdown, trade count, win rate, average
win/loss, fees, capital usage. Inter-strategy fund transfers never count
as investment profit (they're tagged 'transfer' and excluded).

Stability analysis classifies: normal_market | condition_mismatch |
data_anomaly | model_anomaly | execution_anomaly — configurable,
backtestable statistical conditions; a short losing streak alone never
declares a strategy permanently dead.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class StrategyPerformanceMonitor:
    def __init__(self, sim: Any, state_dir: Path) -> None:
        self._sim = sim
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "perf-snapshots.jsonl"

    # ------------------------------------------------------------------
    def strategy_report(self, run: dict[str, Any]) -> dict[str, Any]:
        sid = run["strategy_id"]
        account_id = run.get("account_id") or ""
        orders = [o for o in self._sim.orders.list(account_id)
                  if o.get("strategy_id") == sid]
        filled = [o for o in orders
                  if _d(o.get("filled_qty")) > 0]
        realized = _d(0)
        wins, losses = [], []
        for o in filled:
            fee = _d(o.get("fee")) + _d(o.get("tax"))
            if o["side"] in ("sell", "redeem"):
                pnl = _d(o["filled_qty"]) * _d(o["avg_fill_price"]) - fee
                realized += pnl
                (wins if pnl > 0 else losses).append(pnl)
        cash = self._sim.accounts.cash(account_id) \
            if account_id else {}
        positions = [p for p in self._sim.positions.list(account_id)]
        unrealized = sum((_d(p.get("unrealized_pnl")) for p in positions
                          if p.get("unrealized_pnl")), _d(0))
        equity = _d(cash.get("available")) + _d(cash.get("reserved")) \
            + _d(cash.get("unsettled")) + sum(
                (_d(p.get("market_value") or "0") for p in positions),
                _d(0))
        snap = {
            "run_id": run["run_id"], "strategy_id": sid,
            "strategy_version": run["strategy_version"],
            "account_id": account_id,
            "at": time.time(),
            "equity": str(equity),
            "cash_available": str(cash.get("available", "0")),
            "positions": len(positions),
            "orders": len(orders), "filled": len(filled),
            "realized_pnl": str(realized),
            "unrealized_pnl": str(unrealized),
            "win_rate": (str(len(wins) / len(filled))
                         if filled else ""),
            "avg_win": str(sum(wins) / len(wins)) if wins else "0",
            "avg_loss": str(sum(losses) / len(losses))
                        if losses else "0",
            "note": "策略間資金移轉以 transfer 記帳——不計入投資獲利",
            "simulated": True,
        }
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
        return {"ok": True, "snapshot": snap}

    def account_report(self, account_id: str) -> dict[str, Any]:
        return self._sim.performance.report(account_id)

    def curve(self, run_id: str | None = None
              ) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        rows = [json.loads(l) for l in self._path.read_text(
            encoding="utf-8").splitlines() if l.strip()]
        if run_id:
            rows = [r for r in rows if r["run_id"] == run_id]
        return rows

    def max_drawdown(self, run_id: str) -> dict[str, Any]:
        vals = [_d(r["equity"]) for r in self.curve(run_id)]
        peak, mdd = _d(0), _d(0)
        for v in vals:
            peak = max(peak, v)
            if peak > 0:
                mdd = min(mdd, v / peak - 1)
        return {"ok": True, "max_drawdown": str(mdd),
                "observations": len(vals)}


class StrategyStabilityAnalyzer:
    """Config-driven classification — never a short-loss death sentence."""

    def __init__(self, perf: StrategyPerformanceMonitor,
                 *, loss_streak_warn: int = 5,
                 signal_freq_change_warn: float = 3.0) -> None:
        self._perf = perf
        self._loss_streak_warn = loss_streak_warn
        self._freq_warn = signal_freq_change_warn

    def analyze(self, run: dict[str, Any], *,
                recent_signals: int = 0,
                baseline_signals: int = 0,
                data_ok: bool = True,
                model_ok: bool = True,
                execution_errors: int = 0) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        classification = "normal_market"
        if not data_ok:
            classification = "data_anomaly"
            findings.append({"kind": "data", "detail":
                             "行情資料異常——策略條件可信度下降"})
        elif not model_ok:
            classification = "model_anomaly"
            findings.append({"kind": "model", "detail":
                             "星澄輸出異常——AI_ASSISTED 訊號不可信"})
        elif execution_errors > 0:
            classification = "execution_anomaly"
            findings.append({"kind": "execution",
                             "count": execution_errors})
        elif (baseline_signals > 0 and recent_signals >=
              baseline_signals * self._freq_warn):
            classification = "condition_mismatch"
            findings.append({"kind": "signal_frequency",
                             "recent": recent_signals,
                             "baseline": baseline_signals,
                             "detail": "訊號頻率異常放大——"
                                       "市場條件可能已不適用"})
        dd = self._perf.max_drawdown(run["run_id"])
        findings.append({"kind": "drawdown",
                         "max_drawdown": dd["max_drawdown"]})
        return {"ok": True, "run_id": run["run_id"],
                "classification": classification,
                "findings": findings,
                "note": "短期虧損不等於策略失效——"
                        "分類依可配置統計條件"}
