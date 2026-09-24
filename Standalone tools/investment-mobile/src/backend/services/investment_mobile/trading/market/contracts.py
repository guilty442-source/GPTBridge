"""market-data contracts — normalized quote/candle/source-status types.

All price/volume/notional fields are ``decimal.Decimal`` — floats are
never used for settlement-authoritative numbers. Every record carries
``source_id`` + timestamps with timezone info so consumers can judge
freshness and provenance; ``data_status`` marks delayed/stale/suspect
data explicitly instead of silently pretending real-time quality.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: datetime | str | float | int | None) -> datetime:
    """Normalize to an aware UTC datetime. Naive input is rejected."""
    if value is None:
        return utcnow()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp rejected — timezone required")
        return parsed.astimezone(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("naive timestamp rejected — timezone required")
    return value.astimezone(timezone.utc)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class DataStatus(str, Enum):
    OK = "ok"                    # verified real-time quote
    DELAYED = "delayed"          # source only provides delayed data
    STALE = "stale"              # older than the source's freshness SLA
    SUSPECT = "suspect"          # failed validation (outlier/inconsistent)
    MISSING = "missing"          # expected data absent (gap detected)


class MarketSession(str, Enum):
    PRE = "pre"
    REGULAR = "regular"
    POST = "post"
    CLOSED = "closed"


class ConnectionStatus(str, Enum):
    CONNECTED = "connected"
    DEGRADED = "degraded"        # running but failing repeatedly
    DISCONNECTED = "disconnected"
    RECOVERING = "recovering"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class MarketQuote:
    instrument_id: str
    market: str
    source_id: str
    currency: str
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    last_price: Decimal | None = None
    volume: Decimal = Decimal("0")
    source_timestamp: datetime = field(default_factory=utcnow)
    received_timestamp: datetime = field(default_factory=utcnow)
    market_session: str = MarketSession.REGULAR.value
    data_status: str = DataStatus.OK.value
    quote_id: str = field(default_factory=lambda: _new_id("q"))

    def __post_init__(self) -> None:
        self.source_timestamp = _dt(self.source_timestamp)
        self.received_timestamp = _dt(self.received_timestamp)
        self.bid_price = _dec(self.bid_price)
        self.ask_price = _dec(self.ask_price)
        self.last_price = _dec(self.last_price)
        self.volume = Decimal(str(self.volume or 0))

    @property
    def age_s(self) -> float:
        return (utcnow() - self.received_timestamp).total_seconds()

    @property
    def source_age_s(self) -> float:
        return (utcnow() - self.source_timestamp).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "instrument_id": self.instrument_id,
            "market": self.market,
            "source_id": self.source_id,
            "currency": self.currency,
            "bid_price": str(self.bid_price) if self.bid_price is not None else None,
            "ask_price": str(self.ask_price) if self.ask_price is not None else None,
            "last_price": str(self.last_price) if self.last_price is not None else None,
            "volume": str(self.volume),
            "source_timestamp": self.source_timestamp.isoformat(),
            "received_timestamp": self.received_timestamp.isoformat(),
            "market_session": self.market_session,
            "data_status": self.data_status,
        }


class Timeframe(str, Enum):
    M1 = "1m"
    D1 = "1d"


class AdjustmentType(str, Enum):
    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN = "total_return"


@dataclass
class MarketCandle:
    instrument_id: str
    market: str
    timeframe: str                 # Timeframe value
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    candle_start: datetime
    candle_end: datetime
    source_id: str
    currency: str = ""
    turnover: Decimal = Decimal("0")
    data_revision: int = 1         # bumped when a source corrects history
    adjustment_type: str = AdjustmentType.RAW.value

    def __post_init__(self) -> None:
        for f in ("open", "high", "low", "close", "volume", "turnover"):
            setattr(self, f, Decimal(str(getattr(self, f) or 0)))
        self.candle_start = _dt(self.candle_start)
        self.candle_end = _dt(self.candle_end)
        if self.candle_end <= self.candle_start:
            raise ValueError("candle_end must follow candle_start")

    @property
    def dedup_key(self) -> tuple[str, str, datetime, str]:
        """Identity ignoring revision — corrections share this key."""
        return (
            self.instrument_id,
            self.timeframe,
            self.candle_start,
            self.adjustment_type,
        )

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.low > self.high:
            errs.append("low_above_high")
        if not (self.low <= self.open <= self.high):
            errs.append("open_outside_range")
        if not (self.low <= self.close <= self.high):
            errs.append("close_outside_range")
        if self.volume < 0:
            errs.append("negative_volume")
        return errs

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "market": self.market,
            "timeframe": self.timeframe,
            "open": str(self.open), "high": str(self.high),
            "low": str(self.low), "close": str(self.close),
            "volume": str(self.volume), "turnover": str(self.turnover),
            "currency": self.currency,
            "candle_start": self.candle_start.isoformat(),
            "candle_end": self.candle_end.isoformat(),
            "source_id": self.source_id,
            "data_revision": self.data_revision,
            "adjustment_type": self.adjustment_type,
        }


@dataclass
class MarketDataStatus:
    source_id: str
    connection_status: str = ConnectionStatus.DISCONNECTED.value
    last_update: datetime | None = None
    latency_s: float | None = None
    stale: bool = True
    error_code: str = ""
    recovery_status: str = "idle"  # idle | retrying | resyncing | recovered
    consecutive_failures: int = 0
    last_error_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.last_update is not None:
            self.last_update = _dt(self.last_update)
        if self.last_error_at is not None:
            self.last_error_at = _dt(self.last_error_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "connection_status": self.connection_status,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "latency_s": self.latency_s,
            "stale": self.stale,
            "error_code": self.error_code,
            "recovery_status": self.recovery_status,
            "consecutive_failures": self.consecutive_failures,
            "last_error_at": (
                self.last_error_at.isoformat() if self.last_error_at else None
            ),
        }


def json_decimal_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def dumps_decimal(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=json_decimal_default)
