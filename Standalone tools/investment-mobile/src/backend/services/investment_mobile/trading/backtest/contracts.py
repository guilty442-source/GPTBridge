"""Backtest contracts — config, result, trades, equity curve, PIT records."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BacktestConfig:
    strategy_id: str
    strategy_version: int
    market: str                         # TAIWAN_EQUITY|US_EQUITY|MUTUAL_FUND
    instrument_scope: list[str]
    start_date: date
    end_date: date
    initial_capital: Decimal
    currency: str = "TWD"
    fee_model: str = "broker_default"
    slippage_model: str = "bps"         # none|bps|spread_fraction
    execution_model: str = "next_open"  # next_open|close|limit
    data_revision: str = "latest"
    timeframe: str = "1d"
    parameters: dict[str, Any] = field(default_factory=dict)
    assumptions: dict[str, Any] = field(default_factory=dict)
    account_id: str = "backtest"        # simulated account — never real
    run_id: str = field(default_factory=lambda: _new_id("bt"))

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.end_date <= self.start_date:
            errs.append("DATE_RANGE_INVALID")
        if self.initial_capital <= 0:
            errs.append("CAPITAL_INVALID")
        if not self.instrument_scope:
            errs.append("SCOPE_EMPTY")
        return errs


@dataclass
class BacktestTrade:
    """One simulated fill — never a real execution."""

    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    slippage: Decimal
    signal_bar: int
    fill_bar: int                     # ≥ signal_bar (never same-bar lookahead)
    fill_time: str
    notional: Decimal
    currency: str
    order_type: str = "market"
    fill_kind: str = "full"           # full|partial|none
    reason: str = ""
    simulated: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id, "side": self.side,
            "quantity": str(self.quantity), "price": str(self.price),
            "fee": str(self.fee), "slippage": str(self.slippage),
            "signal_bar": self.signal_bar, "fill_bar": self.fill_bar,
            "fill_time": self.fill_time, "notional": str(self.notional),
            "currency": self.currency, "order_type": self.order_type,
            "fill_kind": self.fill_kind, "reason": self.reason,
            "simulated": True,
        }


@dataclass
class EquityPoint:
    t: str                            # ISO date
    equity: Decimal
    cash: Decimal
    positions_value: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.t, "equity": str(self.equity),
                "cash": str(self.cash),
                "positions_value": str(self.positions_value)}


@dataclass
class BacktestResult:
    config: dict[str, Any]
    initial_capital: str
    final_equity: str
    total_return: str
    annualized_return: str
    max_drawdown: str
    volatility: str
    sharpe: str
    sortino: str
    trade_count: int
    win_rate: str
    avg_win: str
    avg_loss: str
    profit_factor: str
    cost_total: str
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    positions_log: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_revision: str = "latest"
    survivorship_risk: bool = False
    simulated: bool = True
    run_id: str = ""
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "config": self.config,
            "initial_capital": self.initial_capital,
            "final_equity": self.final_equity,
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "max_drawdown": self.max_drawdown,
            "volatility": self.volatility,
            "sharpe": self.sharpe, "sortino": self.sortino,
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "avg_win": self.avg_win, "avg_loss": self.avg_loss,
            "profit_factor": self.profit_factor,
            "cost_total": self.cost_total,
            "equity_curve": self.equity_curve,
            "positions_log": self.positions_log,
            "trades": self.trades,
            "assumptions": self.assumptions,
            "warnings": self.warnings,
            "data_revision": self.data_revision,
            "survivorship_risk": self.survivorship_risk,
            "simulated": True,
            "created_at": self.created_at.isoformat(),
            "note": "模擬結果不代表未來收益",
        }
