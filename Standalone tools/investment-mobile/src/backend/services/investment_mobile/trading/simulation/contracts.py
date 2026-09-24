"""Simulation contracts — paper accounts/orders/fills, shadow signals.

Everything here is *simulated*: ids live in a separate namespace
(paper-*/shadow-*/sim-*), journals are append-only, and no object in
this package references broker adapters or real account ledgers.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> float:
    return time.time()


class PaperOrderStatus:
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    ALL = frozenset({CREATED, VALIDATED, ACCEPTED, PARTIALLY_FILLED,
                     FILLED, CANCELLED, EXPIRED, REJECTED})
    TERMINAL = frozenset({FILLED, CANCELLED, EXPIRED, REJECTED})


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PaperOrderStatus.CREATED: frozenset({
        PaperOrderStatus.VALIDATED, PaperOrderStatus.REJECTED,
        PaperOrderStatus.CANCELLED}),
    PaperOrderStatus.VALIDATED: frozenset({
        PaperOrderStatus.ACCEPTED, PaperOrderStatus.REJECTED,
        PaperOrderStatus.CANCELLED}),
    PaperOrderStatus.ACCEPTED: frozenset({
        PaperOrderStatus.PARTIALLY_FILLED, PaperOrderStatus.FILLED,
        PaperOrderStatus.CANCELLED, PaperOrderStatus.EXPIRED}),
    PaperOrderStatus.PARTIALLY_FILLED: frozenset({
        PaperOrderStatus.PARTIALLY_FILLED, PaperOrderStatus.FILLED,
        PaperOrderStatus.CANCELLED, PaperOrderStatus.EXPIRED}),
    PaperOrderStatus.FILLED: frozenset(),
    PaperOrderStatus.CANCELLED: frozenset(),
    PaperOrderStatus.EXPIRED: frozenset(),
    PaperOrderStatus.REJECTED: frozenset(),
}


class CashState:
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"      # locked by open buy orders
    UNSETTLED = "UNSETTLED"    # sell proceeds pending T+n
    SETTLED = "SETTLED"


@dataclass
class PaperAccount:
    account_id: str
    account_name: str
    market: str                     # TAIWAN_EQUITY|US_EQUITY|MUTUAL_FUND
    base_currency: str
    initial_capital: Decimal
    status: str = "ACTIVE"          # ACTIVE|SUSPENDED|CLOSED
    created_at: float = field(default_factory=_now)
    paper: bool = True

    def __post_init__(self):
        self.initial_capital = Decimal(str(self.initial_capital))

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id, "account_name":
            self.account_name, "market": self.market,
            "base_currency": self.base_currency,
            "initial_capital": str(self.initial_capital),
            "status": self.status, "created_at": self.created_at,
            "paper": True, "simulated": True,
        }


@dataclass
class PaperOrder:
    account_id: str
    instrument_id: str
    side: str                       # buy|sell|subscribe|redeem
    quantity: Decimal
    order_type: str = "market"      # market|limit
    strategy_id: str = ""
    strategy_version: int = 0
    limit_price: Decimal | None = None
    reference_price: Decimal | None = None
    currency: str = ""
    expires_at: float = 0.0         # 0 = good-for-day
    client_order_id: str = ""       # caller dedup key
    order_id: str = field(default_factory=lambda: _new_id("pord"))
    status: str = PaperOrderStatus.CREATED
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    created_at: float = field(default_factory=_now)

    def __post_init__(self):
        self.quantity = Decimal(str(self.quantity))
        self.filled_qty = Decimal(str(self.filled_qty))
        self.avg_fill_price = Decimal(str(self.avg_fill_price))
        if self.limit_price is not None:
            self.limit_price = Decimal(str(self.limit_price))
        if self.reference_price is not None:
            self.reference_price = Decimal(str(self.reference_price))

    @property
    def open_qty(self) -> Decimal:
        return self.quantity - self.filled_qty

    def can_transition(self, target: str) -> bool:
        return target in _ALLOWED_TRANSITIONS.get(self.status,
                                                  frozenset())

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id, "account_id": self.account_id,
            "instrument_id": self.instrument_id, "side": self.side,
            "order_type": self.order_type, "quantity": str(self.quantity),
            "limit_price": str(self.limit_price)
            if self.limit_price is not None else None,
            "reference_price": str(self.reference_price)
            if self.reference_price is not None else None,
            "currency": self.currency, "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "client_order_id": self.client_order_id,
            "status": self.status, "filled_qty": str(self.filled_qty),
            "avg_fill_price": str(self.avg_fill_price),
            "created_at": self.created_at, "expires_at": self.expires_at,
            "simulated": True,
        }


@dataclass
class PaperExecution:
    """A simulated fill — never a broker confirmation."""

    order_id: str
    account_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    market_source: str = ""         # which observation drove the fill
    exec_model: str = "daily_bar"   # daily_bar|orderbook|nav
    assumptions: list[str] = field(default_factory=list)
    event_seq: int = 0              # simulation event sequence (recovery)
    exec_id: str = field(default_factory=lambda: _new_id("pexec"))
    created_at: float = field(default_factory=_now)
    simulated: bool = True

    def __post_init__(self):
        self.quantity = Decimal(str(self.quantity))
        self.price = Decimal(str(self.price))
        self.fee = Decimal(str(self.fee))
        self.slippage = Decimal(str(self.slippage))

    def to_dict(self) -> dict[str, Any]:
        return {
            "exec_id": self.exec_id, "order_id": self.order_id,
            "account_id": self.account_id,
            "instrument_id": self.instrument_id, "side": self.side,
            "quantity": str(self.quantity), "price": str(self.price),
            "fee": str(self.fee), "slippage": str(self.slippage),
            "market_source": self.market_source,
            "exec_model": self.exec_model,
            "assumptions": list(self.assumptions),
            "event_seq": self.event_seq, "created_at": self.created_at,
            "simulated": True,
        }


@dataclass
class PaperPosition:
    account_id: str
    instrument_id: str
    quantity: Decimal = Decimal("0")
    average_cost: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    currency: str = ""
    updated_at: float = field(default_factory=_now)

    def __post_init__(self):
        self.quantity = Decimal(str(self.quantity))
        self.average_cost = Decimal(str(self.average_cost))
        self.realized_pnl = Decimal(str(self.realized_pnl))

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "instrument_id": self.instrument_id,
            "quantity": str(self.quantity),
            "average_cost": str(self.average_cost),
            "realized_pnl": str(self.realized_pnl),
            "currency": self.currency, "updated_at": self.updated_at,
            "simulated": True,
        }


@dataclass
class ShadowSignal:
    """AI signal record — immutable once written."""

    instrument_id: str
    market: str
    side: str                       # BUY|SELL|HOLD|ADD|REDUCE|EXIT|
                                    # SUBSCRIBE|REDEEM|SWITCH
    strategy_id: str = ""
    strategy_version: int = 0
    model_id: str = ""
    model_version: str = ""
    reference_price: Decimal | None = None
    data_revision: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    risk_note: str = ""
    valid_until: str = ""           # ISO date or bars
    signal_id: str = field(default_factory=lambda: _new_id("ssig"))
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "instrument_id": self.instrument_id, "market": self.market,
            "side": self.side, "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "reference_price": str(self.reference_price)
            if self.reference_price is not None else None,
            "data_revision": self.data_revision,
            "evidence_refs": list(self.evidence_refs),
            "risk_note": self.risk_note, "valid_until": self.valid_until,
            "created_at": self.created_at, "simulated": True,
        }


@dataclass
class StrategyRun:
    """One strategy bound to a paper account under the coordinator."""

    strategy_id: str
    strategy_version: int
    paper_account_id: str
    allocated_capital: Decimal
    risk_budget: Decimal = Decimal("0")
    status: str = "running"         # running|paused|stopped|error
    run_id: str = field(default_factory=lambda: _new_id("srun"))
    started_at: float = field(default_factory=_now)

    def __post_init__(self):
        self.allocated_capital = Decimal(str(self.allocated_capital))
        self.risk_budget = Decimal(str(self.risk_budget))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "paper_account_id": self.paper_account_id,
            "allocated_capital": str(self.allocated_capital),
            "risk_budget": str(self.risk_budget),
            "status": self.status, "started_at": self.started_at,
            "simulated": True,
        }
