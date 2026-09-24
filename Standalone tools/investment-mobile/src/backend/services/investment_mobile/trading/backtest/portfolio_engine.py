"""PortfolioBacktestEngine — cross-market/cross-currency replay.

Per-account cash ledgers (TW/US/fund) never auto-mingle: a transfer
between accounts is an explicit simulated event with a historical FX
rate. Positions mark at their own market's confirmed bars; the equity
curve converts at the historical rate row for that date.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..market.fx import CurrencyRateService
from .contracts import BacktestConfig


class PortfolioBacktestEngine:
    def __init__(self, fx: CurrencyRateService) -> None:
        self._fx = fx

    def replay(
        self, config: BacktestConfig,
        legs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """`legs`: per-account leg results from sub-engine runs plus
        explicit cash-transfer events between accounts."""
        accounts: dict[str, Decimal] = {}
        transfers: list[dict[str, Any]] = []
        warnings: list[str] = []

        for leg in legs:
            acct = str(leg.get("account_id") or "backtest")
            ccy = str(leg.get("currency") or config.currency)
            key = f"{acct}:{ccy}"
            accounts[key] = accounts.get(key, Decimal(0)) + Decimal(
                str(leg.get("final_equity") or leg.get("amount") or 0))

        for ev in config.assumptions.get("cash_transfers") or []:
            src = str(ev.get("from") or "")
            dst = str(ev.get("to") or "")
            amt = Decimal(str(ev.get("amount") or 0))
            on = str(ev.get("date") or config.start_date)
            if accounts.get(src, Decimal(0)) < amt:
                warnings.append(f"transfer_blocked:{src}")
                continue
            src_ccy = src.split(":")[-1]
            dst_ccy = dst.split(":")[-1]
            conv = self._fx.convert(amt, src_ccy, dst_ccy)
            rate_note = "historical_fx"
            if isinstance(conv, dict) and conv.get("ok"):
                dst_amt = Decimal(str(conv["amount"]))
            else:
                dst_amt = amt
                warnings.append("fx_missing:used_nominal")
                rate_note = "fx_missing_nominal"
            accounts[src] = accounts.get(src, Decimal(0)) - amt
            accounts[dst] = accounts.get(dst, Decimal(0)) + dst_amt
            transfers.append({
                "from": src, "to": dst, "amount": str(amt),
                "received": str(dst_amt), "on": on, "fx": rate_note,
                "simulated": True,
            })

        total = sum(accounts.values())
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
                "execution_model": "portfolio_replay",
            },
            "accounts": {k: str(v) for k, v in accounts.items()},
            "total_equity": str(total),
            "total_return": "0",
            "trade_count": 0,
            "transfers": transfers,
            "warnings": warnings,
            "simulated": True,
            "data_revision": config.data_revision,
            "note": "跨帳戶資金移轉為明確模擬事件；匯率用歷史時間點",
        }
        return {"ok": True, "result": result}
