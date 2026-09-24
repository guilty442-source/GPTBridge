"""PaperPerformanceService + StrategyBenchmarkService + ShadowPaperComparison.

All outputs are explicitly labelled PAPER PERFORMANCE — never merged
with backtest results or real-account performance.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..intelligence.indicators import max_drawdown, volatility
from ..market.history import CandleStore
from .accounts import PaperAccountService
from .orders import PaperOrderManagementSystem
from .positions import PaperPositionService


class PaperPerformanceService:
    def __init__(self, accounts: PaperAccountService,
                 positions: PaperPositionService,
                 orders: PaperOrderManagementSystem,
                 fund_positions: Any = None) -> None:
        self._accounts = accounts
        self._positions = positions
        self._orders = orders
        # callable(account_id) -> list[dict] — settled fund units valued
        # at latest published NAV; equities stay in PaperPositionService
        self._fund_positions = fund_positions

    def report(self, account_id: str,
               marks: dict[str, Decimal] | None = None) -> dict[str, Any]:
        marks = marks or {}
        cash = self._accounts.cash(account_id)
        acct = self._accounts.get(account_id)
        if acct is None:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        initial = Decimal(acct["initial_capital"])
        realized = Decimal("0")
        positions_value = Decimal("0")
        for p in self._positions.list(account_id):
            realized += Decimal(p["realized_pnl"])
            px = marks.get(p["instrument_id"])
            positions_value += (Decimal(p["quantity"]) * px) \
                if px else Decimal(p["quantity"]) * Decimal(
                    p["average_cost"])
        fund_rows = (self._fund_positions(account_id)
                     if self._fund_positions is not None else [])
        fund_value = sum(
            (Decimal(str(p["market_value"])) for p in fund_rows),
            Decimal("0"))
        fund_realized = sum(
            (Decimal(str(p.get("realized_pnl") or 0))
             for p in fund_rows), Decimal("0"))
        fund_unrealized = sum(
            (Decimal(str(p.get("unrealized_pnl") or 0))
             for p in fund_rows), Decimal("0"))
        positions_value += fund_value
        realized += fund_realized
        total = (Decimal(cash["available"]) + Decimal(cash["reserved"])
                 + Decimal(cash["unsettled"]) + positions_value)
        costs = Decimal("0")
        dividends = Decimal("0")
        for e in self._accounts.ledger(account_id, limit=10_000):
            if e["kind"] in ("fee", "tax"):
                costs += abs(Decimal(e["amount"]))
            if e["kind"] == "dividend":
                dividends += Decimal(e["amount"])

        execs = [x for x in self._orders.executions()
                 if x["account_id"] == account_id]
        ret = (total / initial - 1) if initial > 0 else Decimal("0")

        return {
            "ok": True, "label": "PAPER PERFORMANCE",
            "account_id": account_id,
            "initial_capital": str(initial),
            "total_assets": str(total),
            "cash": cash, "positions_value": str(positions_value),
            "fund_positions_value": str(fund_value),
            "realized_pnl": str(realized),
            "unrealized_pnl": str(positions_value - sum(
                Decimal(p["quantity"]) * Decimal(p["average_cost"])
                for p in self._positions.list(account_id))
                + fund_unrealized - fund_value),
            "total_return": f"{float(ret):.6f}",
            "costs": str(costs), "dividends": str(dividends),
            "trade_count": len(execs),
            "simulated": True,
            "note": "PAPER 績效不得與真實帳戶績效混用",
        }


class StrategyBenchmarkService:
    def __init__(self, candles: CandleStore) -> None:
        self._candles = candles

    def compare(self, equity_curve: list[dict[str, Any]],
                benchmark_id: str, timeframe: str = "1d"
                ) -> dict[str, Any]:
        bars = self._candles.candles(benchmark_id, timeframe)
        if not equity_curve or not bars:
            return {"ok": False, "error_code": "INSUFFICIENT_DATA"}
        bench = [float(b.close) for b in bars]
        strat = [float(Decimal(str(p["equity"]))) for p in equity_curve]
        n = min(len(bench), len(strat))
        bench = bench[:n]
        strat = strat[:n]
        b_ret = bench[-1] / bench[0] - 1 if bench[0] else 0
        s_ret = strat[-1] / strat[0] - 1 if strat[0] else 0
        return {
            "ok": True, "comparable": True,
            "same_window": True,
            "strategy": {
                "total_return": f"{s_ret:.6f}",
                "max_drawdown": max_drawdown(strat),
                "volatility": volatility(strat)},
            "benchmark": {
                "instrument_id": benchmark_id,
                "total_return": f"{b_ret:.6f}",
                "max_drawdown": max_drawdown(bench),
                "volatility": volatility(bench)},
            "excess_return": f"{s_ret - b_ret:.6f}",
            "simulated": True,
        }


class ShadowPaperComparison:
    """Signal vs simulated fill vs market aftermath — three lanes."""

    def compare(self, signal: dict[str, Any],
                executions: list[dict[str, Any]],
                market_end_price: Decimal) -> dict[str, Any]:
        ref = Decimal(str(signal.get("reference_price") or 0))
        if ref <= 0:
            return {"ok": False, "error_code": "NO_REFERENCE_PRICE"}
        fills = [e for e in executions
                 if e["instrument_id"] == signal["instrument_id"]]
        signal_return = (market_end_price / ref - 1)
        out = {
            "ok": True, "signal_id": signal["signal_id"],
            "signal_price": str(ref),
            "signal_return": f"{float(signal_return):.6f}",
            "fills": [],
            "note": "訊號報酬 / 模擬毛報酬 / 淨報酬 三軌分離",
            "simulated": True,
        }
        for e in fills:
            px = Decimal(str(e["price"]))
            fee = Decimal(str(e.get("fee") or 0))
            qty = Decimal(str(e["quantity"]))
            gross = (market_end_price / px - 1) if px > 0 else Decimal(0)
            net = gross - (fee / (px * qty)) if qty > 0 else gross
            slippage = (px / ref - 1) if ref > 0 else Decimal(0)
            out["fills"].append({
                "exec_id": e["exec_id"],
                "fill_price": str(px),
                "slippage_vs_signal": f"{float(slippage):.6f}",
                "gross_return": f"{float(gross):.6f}",
                "net_return": f"{float(net):.6f}",
                "cost_impact": str(fee),
            })
        return out
