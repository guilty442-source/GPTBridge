"""Market data source adapters — replaceable interface + capability matrix.

Every source declares its real capability set (realtime vs delayed,
history depth, sessions). Sources that only provide delayed quotes mark
quotes ``data_status=delayed`` — the delay is never hidden.

Real-source HTTP adapters are intentionally NOT implemented in this
phase: no official feed has been verified. ``SimulatedSource`` provides
reproducible test data; ``ManualImportSource`` accepts operator-entered
observations. A source only gains ``verified=True`` after its official
API + licence are validated through the governed verification path.

Legal-source survey (capability matrix, verification pending):
  TW:
    - twse-openapi  (證交所 OpenAPI, official, EOD delayed, free, verified? pending)
    - tpex-openapi  (櫃買 OpenAPI, official, EOD delayed, pending)
    - fugle         (富果 official API, realtime paid tiers, pending)
    - finmind       (community datasets, non-official — research only)
  US:
    - polygon       (official vendor, realtime paid, pending)
    - alphavantage  (freemium, delayed, pending)
    - tiingo        (official vendor, EOD/intraday paid, pending)
    - broker-feed   (per-broker quote API — separate from trading API;
                     Fubon sub-brokerage quote scope NOT assumed)

Unverified web scraping is never treated as tradeable market data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncIterator

from .contracts import (
    DataStatus,
    MarketCandle,
    MarketQuote,
    utcnow,
)


@dataclass
class SourceCapability:
    """Declared capability of a market data source."""

    source_id: str
    markets: list[str]
    realtime: bool = False
    delayed: bool = False
    delay_minutes: int = 0
    history_timeframes: list[str] = field(default_factory=lambda: ["1d"])
    sessions: list[str] = field(default_factory=lambda: ["regular"])
    verified: bool = False          # official API + licence validated
    legal_basis: str = ""           # licence / terms summary
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "markets": self.markets,
            "realtime": self.realtime,
            "delayed": self.delayed,
            "delay_minutes": self.delay_minutes,
            "history_timeframes": self.history_timeframes,
            "sessions": self.sessions,
            "verified": self.verified,
            "legal_basis": self.legal_basis,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Registered capability matrix — honest about verification state
# ---------------------------------------------------------------------------

SOURCE_CAPABILITIES: dict[str, SourceCapability] = {
    "twse-openapi": SourceCapability(
        source_id="twse-openapi",
        markets=["tw"],
        delayed=True,
        delay_minutes=0,  # EOD official data
        history_timeframes=["1d"],
        legal_basis="TWSE OpenAPI (official public data)",
        notes="official exchange EOD data; intraday requires licensed feed",
    ),
    "tpex-openapi": SourceCapability(
        source_id="tpex-openapi",
        markets=["tw"],
        delayed=True,
        history_timeframes=["1d"],
        legal_basis="TPEx OpenAPI (official public data)",
        notes="official TPEx EOD data",
    ),
    "fugle": SourceCapability(
        source_id="fugle",
        markets=["tw"],
        realtime=True,
        history_timeframes=["1d", "1m"],
        legal_basis="Fugle official API (paid realtime tier)",
        notes="licensed TW realtime quotes — credential required",
    ),
    "polygon": SourceCapability(
        source_id="polygon",
        markets=["us"],
        realtime=True,
        history_timeframes=["1d", "1m"],
        sessions=["pre", "regular", "post"],
        legal_basis="Polygon.io commercial licence",
        notes="US realtime incl. pre/post — paid plan required",
    ),
    "alphavantage": SourceCapability(
        source_id="alphavantage",
        markets=["us"],
        delayed=True,
        delay_minutes=15,
        history_timeframes=["1d", "1m"],
        legal_basis="Alpha Vantage free/premium tiers",
        notes="delayed quotes on free tier",
    ),
    "broker-feed": SourceCapability(
        source_id="broker-feed",
        markets=["tw", "us"],
        realtime=False,
        delayed=True,
        legal_basis="broker-provided quotes",
        notes="scope per broker is NOT assumed — verify separately from trading API",
    ),
    "manual-import": SourceCapability(
        source_id="manual-import",
        markets=["tw", "us", "fund"],
        delayed=True,
        history_timeframes=["1d"],
        verified=True,
        legal_basis="operator-entered observations",
        notes="always available; operator is responsible for data provenance",
    ),
    "simulated": SourceCapability(
        source_id="simulated",
        markets=["tw", "us", "fund"],
        realtime=True,
        history_timeframes=["1d", "1m"],
        verified=True,
        legal_basis="reproducible generated data — testing only",
        notes="never used for real decisions; quotes marked simulated",
    ),
}


class SourceError(Exception):
    """A source-level failure — engine isolates it per-source."""


class MarketDataSource:
    """Replaceable source adapter interface.

    Lifecycle: ``connect`` → ``stream`` (async iterator of quotes) /
    ``fetch_history`` → ``disconnect``. A source that throws is degraded
    by the engine — it never stops the engine or other sources.
    """

    capability: SourceCapability

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def stream(self, instrument_ids: list[str]) -> AsyncIterator[MarketQuote]:
        raise NotImplementedError
        yield  # pragma: no cover
    async def fetch_history(
        self,
        instrument_id: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        cursor: datetime | None = None,
    ) -> list[MarketCandle]:
        raise NotImplementedError

    @property
    def verified(self) -> bool:
        return self.capability.verified


class ManualImportSource(MarketDataSource):
    """Operator-entered quotes/candles — always available, flagged delayed."""

    capability = SOURCE_CAPABILITIES["manual-import"]

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    def submit_quote(self, payload: dict[str, Any]) -> MarketQuote:
        return MarketQuote(
            instrument_id=str(payload["instrument_id"]),
            market=str(payload["market"]),
            source_id=self.capability.source_id,
            currency=str(payload.get("currency") or ""),
            bid_price=payload.get("bid_price"),
            ask_price=payload.get("ask_price"),
            last_price=payload.get("last_price"),
            volume=payload.get("volume") or 0,
            source_timestamp=payload.get("source_timestamp"),
            market_session=str(payload.get("market_session") or "regular"),
            data_status=DataStatus.DELAYED.value,
        )

    async def fetch_history(
        self,
        instrument_id: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        cursor: datetime | None = None,
    ) -> list[MarketCandle]:
        return []  # manual history arrives via import_candle calls


class SimulatedSource(MarketDataSource):
    """Deterministic reproducible source for tests — clearly marked."""

    capability = SOURCE_CAPABILITIES["simulated"]

    def __init__(self, seed: int = 7) -> None:
        self._seed = seed
        self._connected = False
        self._fail_next = 0  # fault-injection counter for isolation tests

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def inject_failures(self, count: int) -> None:
        self._fail_next = count

    async def stream(self, instrument_ids: list[str]) -> AsyncIterator[MarketQuote]:
        tick = 0
        while self._connected:
            if self._fail_next:
                self._fail_next -= 1
                raise SourceError("simulated source failure")
            tick += 1
            for iid in instrument_ids:
                market = iid.split(":", 1)[0] if ":" in iid else "tw"
                price = Decimal("100") + Decimal((tick + self._seed) % 50)
                yield MarketQuote(
                    instrument_id=iid,
                    market=market,
                    source_id=self.capability.source_id,
                    currency="TWD" if market == "tw" else "USD",
                    last_price=price,
                    volume=Decimal(1000 * tick),
                    market_session="regular",
                    data_status=DataStatus.OK.value,
                )
            if tick >= 3:
                return

    async def fetch_history(
        self,
        instrument_id: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        cursor: datetime | None = None,
    ) -> list[MarketCandle]:
        """Deterministic daily candles — same input always reproduces."""
        if self._fail_next:
            self._fail_next -= 1
            raise SourceError("simulated source failure")
        from datetime import timedelta

        candles: list[MarketCandle] = []
        market = instrument_id.split(":", 1)[0] if ":" in instrument_id else "tw"
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        n = 0
        while day <= end:
            if day.weekday() < 5:
                base = Decimal(100 + (day.day + self._seed) % 40)
                c_start = day.replace(hour=9)
                candles.append(MarketCandle(
                    instrument_id=instrument_id,
                    market=market,
                    timeframe=timeframe,
                    open=base,
                    high=base + Decimal("3"),
                    low=base - Decimal("2"),
                    close=base + Decimal("1"),
                    volume=Decimal(10_000 + n),
                    turnover=base * Decimal(10_000 + n),
                    candle_start=c_start,
                    candle_end=c_start + timedelta(hours=6),
                    source_id=self.capability.source_id,
                    currency="TWD" if market == "tw" else "USD",
                ))
                n += 1
            day += timedelta(days=1)
        if cursor is not None:
            candles = [c for c in candles if c.candle_start > cursor]
        return candles
