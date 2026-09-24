"""MarketDataSubscriptionManager — one shared feed per instrument (§5).

Multiple consumers (strategies, monitors, UI, AI analysis) subscribe to a
single per-instrument feed. Delivery follows the event contract —
subscribers get the same update once. Subscribers have lifecycles:

- pinned (running strategies) — feed stays live even if UI closes
- transient (UI views, ad-hoc analysis) — released when unsubscribed;
  the feed itself stops when no pinned or transient subscribers remain.
"""
from __future__ import annotations

import time
from typing import Any, Callable

Subscriber = Callable[[dict[str, Any]], None]


class MarketDataSubscriptionManager:
    def __init__(self) -> None:
        self._subs: dict[str, dict[str, dict[str, Any]]] = {}
        self._active_feeds: set[str] = set()
        self._deliveries = 0
        self._next = 0

    def subscribe(self, instrument_id: str, fn: Subscriber, *,
                  pinned: bool = False, owner: str = "") -> str:
        iid = str(instrument_id)
        self._next += 1
        sid = f"sub-{self._next}"
        self._subs.setdefault(iid, {})[sid] = {
            "fn": fn, "pinned": bool(pinned), "owner": owner,
            "subscribed_at": time.time(),
        }
        self._active_feeds.add(iid)
        return sid

    def unsubscribe(self, sid: str) -> bool:
        for iid, subs in list(self._subs.items()):
            if sid in subs:
                del subs[sid]
                if not subs:
                    del self._subs[iid]
                    self._active_feeds.discard(iid)
                return True
        return False

    def release_owner(self, owner: str) -> int:
        """Drop every transient subscription owned by e.g. a closed UI
        page. Pinned strategy subscriptions survive."""
        removed = 0
        for iid, subs in list(self._subs.items()):
            for sid, s in list(subs.items()):
                if s["owner"] == owner and not s["pinned"]:
                    del subs[sid]
                    removed += 1
            if not subs:
                del self._subs[iid]
                self._active_feeds.discard(iid)
        return removed

    def publish(self, instrument_id: str, update: dict[str, Any]) -> int:
        """Deliver one update to all subscribers; a failing transient
        subscriber is removed but never blocks the feed."""
        subs = self._subs.get(str(instrument_id))
        if not subs:
            return 0
        delivered = 0
        for sid, s in list(subs.items()):
            try:
                s["fn"](update)
                delivered += 1
            except Exception:
                if not s["pinned"]:
                    del subs[sid]
        if not subs:
            self._subs.pop(str(instrument_id), None)
            self._active_feeds.discard(str(instrument_id))
        self._deliveries += delivered
        return delivered

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "feeds": sorted(self._active_feeds),
            "subscribers": {k: len(v) for k, v in self._subs.items()},
            "pinned": sum(1 for subs in self._subs.values()
                          for s in subs.values() if s["pinned"]),
            "deliveries": self._deliveries,
            "note": "one feed per instrument; pinned strategy subs "
                    "survive UI close",
        }
