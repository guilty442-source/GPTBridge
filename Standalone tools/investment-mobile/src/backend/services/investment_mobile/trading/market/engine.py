"""MarketDataEngine — centralized quote ingestion.

One bounded queue + one dispatcher task serves ALL instruments and ALL
sources — no per-stock processes, no per-stock threads. Per-source
stream tasks are fault-isolated: a failing source degrades itself and
is rescheduled with bounded backoff; the engine and other sources keep
running.

Subscription lifecycle: ``subscribe``/``unsubscribe`` are refcounted by
consumer id; a source stream is only opened while at least one
subscription exists for its instruments — idle sources release their
resources.

Independence: the engine never imports or consults model code — market
data flows regardless of 星澄 availability.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .contracts import (
    ConnectionStatus,
    DataStatus,
    MarketDataStatus,
    MarketQuote,
    dumps_decimal,
    utcnow,
)
from .quality import QuoteValidator
from .sources import MarketDataSource


class MarketDataEngine:
    """Centralized ingestion + shared cache + bounded notify."""

    QUEUE_MAX = 10_000           # bounded — backpressure, never unbounded
    STALE_AFTER_S = 30.0         # realtime freshness SLA
    RETRY_MAX = 5
    RETRY_BACKOFF_S = 5.0

    def __init__(self, state_dir: Path, audit: Any | None = None) -> None:
        self._dir = Path(state_dir)
        self._audit = audit
        self._quotes_path = self._dir / "market-quotes.jsonl"
        self._status_path = self._dir / "market-source-status.json"
        self._sources: dict[str, MarketDataSource] = {}
        self._status: dict[str, MarketDataStatus] = {}
        self._latest: dict[str, MarketQuote] = {}
        self._subscriptions: dict[str, set[str]] = defaultdict(set)
        self._queue: asyncio.Queue[MarketQuote] | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._dispatcher: asyncio.Task | None = None
        self._listeners: list[Callable[[MarketQuote], Any]] = []
        self._flush_listeners: list[Callable[[list[dict[str, Any]]], Any]] = []
        self._validator = QuoteValidator()
        self._running = False
        self._load_latest()

    # ------------------------------------------------------------------
    def _load_latest(self) -> None:
        try:
            lines = self._quotes_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for raw in lines[-2000:]:
            try:
                row = json.loads(raw)
                quote = MarketQuote(
                    instrument_id=row["instrument_id"], market=row["market"],
                    source_id=row["source_id"], currency=row["currency"],
                    bid_price=row.get("bid_price"),
                    ask_price=row.get("ask_price"),
                    last_price=row.get("last_price"),
                    volume=row.get("volume") or 0,
                    source_timestamp=row.get("source_timestamp"),
                    received_timestamp=row.get("received_timestamp"),
                    market_session=row.get("market_session", "regular"),
                    data_status=row.get("data_status", "ok"),
                    quote_id=row.get("quote_id", ""),
                )
                self._latest[quote.instrument_id] = quote
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    def _persist_status(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._status_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {k: s.to_dict() for k, s in self._status.items()},
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._status_path)

    # ------------------------------------------------------------------
    # Source management
    # ------------------------------------------------------------------
    def register_source(self, source: MarketDataSource) -> None:
        self._sources[source.capability.source_id] = source
        self._status.setdefault(
            source.capability.source_id,
            MarketDataStatus(source_id=source.capability.source_id),
        )

    def source_status(self, source_id: str | None = None) -> list[dict[str, Any]]:
        if source_id:
            s = self._status.get(source_id)
            return [s.to_dict()] if s else []
        return [s.to_dict() for s in self._status.values()]

    def capability_matrix(self) -> list[dict[str, Any]]:
        return [s.capability.to_dict() for s in self._sources.values()]

    # ------------------------------------------------------------------
    # Subscription lifecycle (refcounted by consumer id)
    # ------------------------------------------------------------------
    def subscribe(self, consumer_id: str, instrument_ids: list[str]) -> dict[str, Any]:
        for iid in instrument_ids:
            self._subscriptions[str(iid)].add(str(consumer_id))
        self._ensure_streams()
        return {"ok": True, "subscriptions": self.active_subscriptions()}

    def unsubscribe(self, consumer_id: str, instrument_ids: list[str]) -> dict[str, Any]:
        for iid in instrument_ids:
            subs = self._subscriptions.get(str(iid))
            if subs:
                subs.discard(str(consumer_id))
                if not subs:
                    del self._subscriptions[str(iid)]
        self._ensure_streams()
        return {"ok": True, "subscriptions": self.active_subscriptions()}

    def active_subscriptions(self) -> dict[str, list[str]]:
        return {iid: sorted(c) for iid, c in self._subscriptions.items()}

    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        self._dispatcher = asyncio.create_task(self._dispatch_loop())
        self._ensure_streams()

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks.values()):
            task.cancel()
        for source in self._sources.values():
            try:
                await source.disconnect()
            except Exception:
                pass
        if self._dispatcher:
            self._dispatcher.cancel()
        self._tasks.clear()
        self._running = False

    # ------------------------------------------------------------------
    def _wanted_instruments(self, market: str) -> list[str]:
        return [iid for iid in self._subscriptions if iid.startswith(f"{market}:")]

    def _ensure_streams(self) -> None:
        """Open source streams only while subscriptions demand them."""
        if not self._running:
            return
        for source_id, source in self._sources.items():
            wanted: list[str] = []
            for market in source.capability.markets:
                wanted.extend(self._wanted_instruments(market))
            running = self._tasks.get(source_id)
            alive = running is not None and not running.done()
            if wanted and not alive:
                self._tasks[source_id] = asyncio.create_task(
                    self._source_loop(source)
                )
            elif not wanted and alive:
                running.cancel()  # release the stream — no consumers
                self._tasks.pop(source_id, None)

    # ------------------------------------------------------------------
    async def _source_loop(self, source: MarketDataSource) -> None:
        """Per-source isolated stream with bounded retry."""
        sid = source.capability.source_id
        status = self._status[sid]
        attempts = 0
        while self._running and attempts <= self.RETRY_MAX:
            try:
                if status.connection_status != ConnectionStatus.CONNECTED.value:
                    await source.connect()
                    status.connection_status = ConnectionStatus.CONNECTED.value
                    status.recovery_status = "recovered" if attempts else "idle"
                instruments: list[str] = []
                for market in source.capability.markets:
                    instruments.extend(self._wanted_instruments(market))
                if not instruments:
                    return
                async for quote in source.stream(instruments):
                    if not self._running:
                        return
                    if self._queue and not self._queue.full():
                        self._queue.put_nowait(quote)
                    attempts = 0
                    status.stale = False
                    status.last_update = utcnow()
                    status.error_code = ""
                return  # clean stream end
            except asyncio.CancelledError:
                # Cancelled mid-failure means the connection is gone for
                # good — report it truthfully instead of leaving a stale
                # "degraded" that implies retries are still running.  A
                # healthy stream keeps its last observed "connected".
                if status.connection_status != ConnectionStatus.CONNECTED.value:
                    status.connection_status = ConnectionStatus.DISCONNECTED.value
                    status.recovery_status = "idle"
                    self._persist_status()
                return
            except Exception as exc:  # fault isolation boundary
                attempts += 1
                status.consecutive_failures += 1
                status.error_code = type(exc).__name__
                status.last_error_at = utcnow()
                status.recovery_status = "retrying"
                status.connection_status = (
                    ConnectionStatus.DEGRADED.value
                    if attempts <= self.RETRY_MAX
                    else ConnectionStatus.DISCONNECTED.value
                )
                self._persist_status()
                if self._audit:
                    self._audit.record("market.source_error", {
                        "source_id": sid,
                        "error": type(exc).__name__,
                        "attempt": attempts,
                    })
                await asyncio.sleep(self.RETRY_BACKOFF_S * attempts)
        if attempts > self.RETRY_MAX:
            status.recovery_status = "idle"
            status.connection_status = ConnectionStatus.DISCONNECTED.value
            status.stale = True
            self._persist_status()

    # ------------------------------------------------------------------
    async def _dispatch_loop(self) -> None:
        """Single dispatcher: validate → cache → persist → notify."""
        assert self._queue is not None
        batch: list[MarketQuote] = []
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    quote = await asyncio.wait_for(self._queue.get(), timeout=0.25)
                    batch.append(quote)
                except asyncio.TimeoutError:
                    pass
                if batch and (len(batch) >= 200 or time.monotonic() - last_flush > 0.5):
                    self._flush(batch)
                    batch.clear()
                    last_flush = time.monotonic()
        finally:
            if batch:
                self._flush(batch)  # never drop buffered quotes on shutdown

    def _flush(self, batch: list[MarketQuote]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with self._quotes_path.open("a", encoding="utf-8") as fh:
            for quote in batch:
                issues = self._validator.check(quote, self._latest.get(quote.instrument_id))
                if issues:
                    quote.data_status = DataStatus.SUSPECT.value
                    if self._audit:
                        self._audit.record("market.quote_suspect", {
                            "instrument_id": quote.instrument_id,
                            "issues": issues,
                            "source_id": quote.source_id,
                        })
                self._latest[quote.instrument_id] = quote
                fh.write(dumps_decimal(quote.to_dict()) + "\n")
                for listener in list(self._listeners):
                    try:
                        listener(quote)
                    except Exception:
                        continue  # a bad listener never stops dispatch
        batch_dicts = [q.to_dict() for q in batch]
        for listener in list(self._flush_listeners):
            try:
                listener(batch_dicts)
            except Exception:
                continue

    # ------------------------------------------------------------------
    def ingest_quote(self, quote: MarketQuote) -> dict[str, Any]:
        """Public entry for manual/imported quotes — goes through the same
        validation → cache → persist → notify path as streamed quotes."""
        if self._running and self._queue is not None:
            if self._queue.full():
                return {"ok": False, "error_code": "QUEUE_BACKPRESSURE"}
            self._queue.put_nowait(quote)
        else:
            self._flush([quote])
        return {"ok": True, "quote": quote.to_dict()}

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------
    def on_quote(self, listener: Callable[[MarketQuote], Any]) -> None:
        self._listeners.append(listener)

    def on_flush(self, listener: Callable[[list[dict[str, Any]]], Any]) -> None:
        """Batch-level hook — used for business-layer mirroring."""
        self._flush_listeners.append(listener)

    def latest_quote(self, instrument_id: str, max_age_s: float | None = None) -> dict[str, Any] | None:
        quote = self._latest.get(str(instrument_id))
        if quote is None:
            return None
        data = quote.to_dict()
        if max_age_s is not None and quote.age_s > max_age_s:
            data["data_status"] = DataStatus.STALE.value
        return data

    def fresh_price(self, instrument_id: str, max_age_s: float) -> dict[str, Any]:
        """Fail-closed price for trading paths — stale data is refused,
        never fabricated."""
        quote = self._latest.get(str(instrument_id))
        if quote is None:
            return {"ok": False, "error_code": "NO_QUOTE"}
        if quote.age_s > max_age_s or quote.source_age_s > max_age_s:
            return {
                "ok": False,
                "error_code": "STALE_MARKET_DATA",
                "age_s": quote.age_s,
                "max_age_s": max_age_s,
            }
        if quote.data_status in (DataStatus.SUSPECT.value, DataStatus.MISSING.value):
            return {
                "ok": False,
                "error_code": "QUOTE_UNRELIABLE",
                "data_status": quote.data_status,
            }
        if quote.last_price is None:
            return {"ok": False, "error_code": "NO_PRICE"}
        return {
            "ok": True,
            "price": str(quote.last_price),
            "data_status": quote.data_status,
            "source_id": quote.source_id,
            "source_timestamp": quote.source_timestamp.isoformat(),
        }

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "running": self._running,
            "sources": self.source_status(),
            "subscriptions": self.active_subscriptions(),
            "cached_instruments": len(self._latest),
            "queue_size": self._queue.qsize() if self._queue else 0,
        }
