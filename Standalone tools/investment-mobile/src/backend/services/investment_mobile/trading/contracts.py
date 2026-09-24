"""Trading engine contracts — shared value objects for the engine cluster.

All objects are plain dataclasses with ``to_dict`` projections so the same
shapes cross the Python façade, the C/C++/C# native components, and the
governed channel payloads.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TradingMode(str, Enum):
    """Operating modes ordered by autonomy. Default is ANALYSIS."""

    ANALYSIS = "ANALYSIS"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    CREATED = "created"
    RISK_REJECTED = "risk_rejected"
    MODE_BLOCKED = "mode_blocked"
    ADAPTER_DENIED = "adapter_denied"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Signal:
    """Candidate trade signal produced by 星澄 analysis or strategy research.

    Signals are advisory only — only the strategy/risk engines may turn them
    into formal order intents.
    """

    instrument: str
    market: str  # "tw" | "us" | "fund"
    side: str = OrderSide.BUY.value
    confidence: float = 0.0
    price: float | None = None
    quantity: float = 0.0
    rationale: str = ""
    source: str = "xingcheng"
    signal_id: str = field(default_factory=lambda: _new_id("sig"))
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderIntent:
    """Formal trade proposal emitted by the strategy engine."""

    instrument: str
    market: str
    side: str
    quantity: float
    price: float | None = None
    strategy_id: str = ""
    signal_id: str = ""
    notional: float = 0.0
    intent_id: str = field(default_factory=lambda: _new_id("int"))
    created_at: float = field(default_factory=_now)

    def effective_notional(self) -> float:
        if self.notional > 0:
            return self.notional
        return float(self.quantity) * float(self.price or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RiskDecision:
    """Fail-closed outcome of the risk engine."""

    approved: bool
    reasons: list[str] = field(default_factory=list)
    limits_checked: list[str] = field(default_factory=list)
    evaluated_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Order:
    """Managed order in the OMS state machine."""

    intent: OrderIntent
    status: str = OrderStatus.CREATED.value
    order_id: str = field(default_factory=lambda: _new_id("ord"))
    broker_order_id: str = ""
    rejection: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["intent"] = self.intent.to_dict()
        return data


@dataclass
class Fill:
    """Execution report (simulated in PAPER/SHADOW, broker-reported in LIVE)."""

    order_id: str
    instrument: str
    market: str
    side: str
    quantity: float
    price: float
    simulated: bool = True
    fill_id: str = field(default_factory=lambda: _new_id("fill"))
    executed_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    instrument: str
    market: str
    quantity: float = 0.0
    average_cost: float = 0.0
    last_price: float | None = None

    @property
    def market_value(self) -> float:
        return self.quantity * float(self.last_price or self.average_cost)

    @property
    def notional(self) -> float:
        return self.quantity * self.average_cost

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["market_value"] = self.market_value
        return data
