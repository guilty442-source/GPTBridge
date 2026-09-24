"""TradingEventDispatcher — typed market/system events with idempotent
delivery.

Event contract:
    event_id, event_type, market, instrument_id, timestamp,
    source_id, data_revision

Every consumer is idempotent: the same event (same fingerprint =
type + instrument + source + revision) delivered twice must not make a
strategy loop emit duplicate trades. Delivery is tracked per consumer,
so a replayed market event is a no-op for consumers that already saw it.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

EVENT_TYPES = frozenset({
    "MARKET_OPEN", "MARKET_CLOSE", "QUOTE_UPDATED", "CANDLE_CLOSED",
    "CORPORATE_ACTION", "FUND_NAV_UPDATED", "PORTFOLIO_CHANGED",
    "RISK_LIMIT_TRIGGERED", "STRATEGY_SIGNAL", "SYSTEM_RECOVERED",
})


def event_fingerprint(event_type: str, instrument_id: str,
                      source_id: str, data_revision: str) -> str:
    raw = f"{event_type}|{instrument_id}|{source_id}|{data_revision}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class TradingEventDispatcher:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "events.jsonl"
        self._fh = open(self._path, "a", encoding="utf-8")
        self._events: dict[str, dict[str, Any]] = {}      # fp → event
        self._delivered: set[str] = set()               # fp|consumer
        self._consumers: dict[str, Callable] = {}       # name → handler
        self._replay()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(
                encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("op") == "delivered":
                self._delivered.add(e.get("key", ""))
                continue
            self._events[e["fingerprint"]] = e

    # ------------------------------------------------------------------
    def publish(self, event_type: str, *, market: str = "",
                instrument_id: str = "", source_id: str = "system",
                data_revision: str = "",
                timestamp: float | None = None,
                payload: dict[str, Any] | None = None
                ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            return {"ok": False, "error_code": "EVENT_TYPE_UNKNOWN"}
        ts = float(timestamp or time.time())
        rev = str(data_revision or "")
        fp = event_fingerprint(event_type, instrument_id,
                               source_id, rev)
        if fp in self._events:
            # replayed event — idempotent no-op, never re-triggers
            return {"ok": True, "event": dict(self._events[fp]),
                    "duplicate": True}
        ev = {
            "event_id": f"tev-{uuid.uuid4().hex[:12]}",
            "fingerprint": fp,
            "event_type": event_type,
            "market": str(market),
            "instrument_id": str(instrument_id),
            "timestamp": ts,
            "source_id": str(source_id),
            "data_revision": rev,
            "payload": dict(payload or {}),
        }
        self._events[fp] = ev
        self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._fh.flush()
        return {"ok": True, "event": dict(ev), "duplicate": False}

    # ------------------------------------------------------------------
    def subscribe(self, consumer_id: str,
                  handler: Callable[[dict[str, Any]], Any]) -> None:
        self._consumers[str(consumer_id)] = handler

    def unsubscribe(self, consumer_id: str) -> None:
        self._consumers.pop(str(consumer_id), None)

    def dispatch(self, event: dict[str, Any]) -> dict[str, Any]:
        """Deliver to every consumer once — same fingerprint re-delivery
        is skipped per consumer (idempotent consumption)."""
        fp = event["fingerprint"]
        results = {}
        for cid, handler in self._consumers.items():
            key = f"{fp}|{cid}"
            if key in self._delivered:
                results[cid] = {"skipped": "duplicate"}
                continue
            try:
                r = handler(dict(event))
                results[cid] = r if isinstance(r, dict) else {"ok": True}
            except Exception as exc:
                results[cid] = {"ok": False,
                                "error": f"{type(exc).__name__}: {exc}"}
            self._delivered.add(key)
            self._fh.write(json.dumps(
                {"op": "delivered", "key": key, "at": time.time()},
                ensure_ascii=False) + "\n")
            self._fh.flush()
        return {"ok": True, "delivered": results}

    def list(self, *, event_type: str | None = None,
             market: str | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        out = [e for e in self._events.values()
               if (event_type is None or e["event_type"] == event_type)
               and (market is None or e["market"] == market)]
        return sorted(out, key=lambda e: e["timestamp"],
                      reverse=True)[:limit]
