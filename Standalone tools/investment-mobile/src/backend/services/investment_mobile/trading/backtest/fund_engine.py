"""FundBacktestEngine — NAV-priced fund strategy replay.

Fund rules differ fundamentally from equity: orders are priced at the
*published* NAV of the pricing date, not the application date; cutoff
times and settlement lags apply. This engine never assumes same-day NAV
fills and never uses unpublished NAVs.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..fund.engine import MutualFundEngine
from ..strategy.contracts import StrategyDefinition
from ..strategy.signals import generate
from .contracts import BacktestConfig, BacktestTrade
from .pit import nav_available_at


class FundBacktestEngine:
    def __init__(self, fund_engine: MutualFundEngine,
                 publish_delay_days: int = 1,
                 settlement_lag_days: int = 3) -> None:
        self._fund = fund_engine
        self._delay = int(publish_delay_days)
        self._settle = int(settlement_lag_days)

    # ------------------------------------------------------------------
    def run(self, config: BacktestConfig,
            strategy: StrategyDefinition) -> dict[str, Any]:
        errors = config.validate() + strategy.validate()
        if errors:
            return {"ok": False, "errors": errors}
        if strategy.market != "MUTUAL_FUND":
            return {"ok": False, "error_code": "NOT_FUND_STRATEGY"}

        cash = Decimal(str(config.initial_capital))
        units: dict[str, Decimal] = {}
        trades: list[dict[str, Any]] = []
        curve: list[dict[str, Any]] = []
        assumptions = [
            "nav_priced_at_next_published",
            f"publish_delay_days={self._delay}",
            f"settlement_lag_days={self._settle}",
        ]

        for iid in config.instrument_scope:
            parts = iid.split(":")
            fid = parts[1] if len(parts) > 1 else iid
            cls = parts[2] if len(parts) > 2 else "A"
            navs = [n for n in self._fund.nav.history(fid, cls)
                    if config.start_date <= n.nav_date <= config.end_date]
            navs.sort(key=lambda n: n.nav_date)
            if len(navs) < 2:
                continue
            closes = [float(n.nav) for n in navs]
            signals = generate(strategy.strategy_type, closes,
                               params=strategy.parameters)

            # PIT: signal at nav index i is only visible after publish
            sig_idx = {s.bar: s for s in signals}
            for i, n in enumerate(navs):
                avail = nav_available_at(n.nav_date, self._delay)
                if i in sig_idx:
                    sig = sig_idx[i]
                    # apply at NEXT published NAV — never same-day
                    fill_idx = i + 1
                    if fill_idx < len(navs):
                        fill_nav = navs[fill_idx]
                        side = sig.side
                        if side in ("subscribe", "buy", "add"):
                            amount = Decimal(
                                str(strategy.parameters.get(
                                    "amount", "3000")))
                            fee_rate = Decimal(
                                str(strategy.parameters.get(
                                    "fee_rate", "0.015")))
                            fee = amount * fee_rate
                            if cash < amount + fee:
                                trades.append(self._row(
                                    iid, side, Decimal(0), fill_nav, i,
                                    fill_idx, "insufficient_cash"))
                            else:
                                qty = amount / fill_nav.nav
                                cash -= amount + fee
                                units[iid] = units.get(iid, Decimal(0)) + qty
                                trades.append(self._row(
                                    iid, side, qty, fill_nav, i, fill_idx,
                                    "ok", fee=fee))
                        elif side in ("redeem", "sell", "reduce"):
                            held = units.get(iid, Decimal(0))
                            qty = min(held, Decimal(str(
                                strategy.parameters.get("quantity",
                                                        held))))
                            if qty > 0:
                                proceeds = qty * fill_nav.nav
                                cash += proceeds
                                units[iid] = held - qty
                                trades.append(self._row(
                                    iid, side, qty, fill_nav, i, fill_idx,
                                    "ok"))

                # equity mark at published NAV
                mv = units.get(iid, Decimal(0)) * n.nav
                curve.append({"t": n.nav_date.isoformat(),
                              "equity": str(cash + mv),
                              "cash": str(cash), "units_value": str(mv)})

        final = Decimal(curve[-1]["equity"]) if curve else Decimal(
            str(config.initial_capital))
        ret = (final / Decimal(str(config.initial_capital)) - 1) \
            if curve else Decimal("0")
        result = {
            "run_id": config.run_id,
            "config": {
                "strategy_id": config.strategy_id,
                "strategy_version": config.strategy_version,
                "market": config.market,
                "instrument_scope": config.instrument_scope,
                "start_date": str(config.start_date),
                "end_date": str(config.end_date),
                "initial_capital": str(config.initial_capital),
                "currency": config.currency,
                "execution_model": "nav_priced",
                "fee_model": config.fee_model,
                "slippage_model": "nav",
            },
            "initial_capital": str(config.initial_capital),
            "final_equity": str(final),
            "total_return": f"{float(ret):.6f}",
            "trade_count": len([t for t in trades
                                if t["status"] == "ok"]),
            "trades": trades, "equity_curve": curve,
            "assumptions": assumptions,
            "simulated": True,
            "data_revision": config.data_revision,
            "note": "NAV 計價非即時成交；模擬不代表實際績效",
        }
        return {"ok": True, "result": result}

    @staticmethod
    def _row(iid, side, qty, nav_row, sig_i, fill_i, status,
             fee=Decimal("0")) -> dict[str, Any]:
        return {
            "instrument_id": iid, "side": side,
            "quantity": str(qty), "nav": str(nav_row.nav),
            "nav_date": nav_row.nav_date.isoformat(),
            "signal_bar": sig_i, "fill_bar": fill_i,
            "status": status, "fee": str(fee), "simulated": True,
        }
