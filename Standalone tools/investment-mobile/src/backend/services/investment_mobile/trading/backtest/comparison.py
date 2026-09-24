"""BacktestComparisonService — same-basis comparisons only.

Two results are only comparable when market, window, currency, cost and
slippage assumptions match; mismatched runs are reported with explicit
`not_comparable` reasons instead of a fake head-to-head table.
"""

from __future__ import annotations

from typing import Any


class BacktestComparisonService:
    _BASIS_KEYS = ("market", "start_date", "end_date", "currency",
                   "execution_model", "fee_model", "slippage_model")

    def compare(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        if len(results) < 2:
            return {"ok": False, "error_code": "NEED_TWO_RUNS"}
        base = results[0].get("config") or {}
        mismatches: list[dict[str, Any]] = []
        for r in results[1:]:
            cfg = r.get("config") or {}
            diffs = [k for k in self._BASIS_KEYS
                     if str(cfg.get(k)) != str(base.get(k))]
            if diffs:
                mismatches.append({
                    "run_id": r.get("run_id"), "diffs": diffs})
        comparable = not mismatches
        table = []
        for r in results:
            table.append({
                "run_id": r.get("run_id"),
                "strategy_id": (r.get("config") or {}).get("strategy_id"),
                "total_return": r.get("total_return"),
                "max_drawdown": r.get("max_drawdown"),
                "sharpe": r.get("sharpe"),
                "trade_count": r.get("trade_count"),
                "cost_total": r.get("cost_total"),
                "survivorship_risk": r.get("survivorship_risk", False),
            })
        return {
            "ok": True, "comparable": comparable,
            "mismatched_basis": mismatches,
            "results": table,
            "note": "不同資料範圍/成本假設不視為同條件比較"
            if not comparable else "",
        }
