"""InvestmentCachePolicy — bounded TTL cache with source versioning (§11).

May cache: historical quotes, indicators, fund metadata, research
results, completed backtests. A cache is NEVER the authority for
accounts/holdings/fills/trade authorization — callers must still consult
the engine for those. Entries carry source_version; a data-source
correction bumps the revision and invalidates affected entries.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class InvestmentCachePolicy:
    def __init__(self, *, max_entries: int = 2000,
                 default_ttl_s: float = 300.0) -> None:
        self._max = max(1, int(max_entries))
        self._ttl = float(default_ttl_s)
        self._store: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._revisions: dict[str, int] = {}
        self._stats = {"hit": 0, "miss": 0, "expired": 0,
                       "invalidated": 0, "evicted": 0}

    def revision(self, source: str) -> int:
        return self._revisions.get(source, 0)

    def bump_revision(self, source: str) -> int:
        """Call on any source correction — invalidates dependent keys."""
        rev = self._revisions.get(source, 0) + 1
        self._revisions[source] = rev
        drop = [k for k, e in self._store.items()
                if e["source"] == source and e["rev"] < rev]
        for k in drop:
            del self._store[k]
            self._stats["invalidated"] += 1
        return rev

    def get(self, key: str) -> Any:
        e = self._store.get(key)
        if e is None:
            self._stats["miss"] += 1
            return None
        if e["expires_at"] < time.time() or \
                e["rev"] != self._revisions.get(e["source"], 0):
            del self._store[key]
            self._stats[
                "expired" if e["expires_at"] < time.time()
                else "invalidated"] += 1
            return None
        self._store.move_to_end(key)
        self._stats["hit"] += 1
        return e["value"]

    def put(self, key: str, value: Any, *, source: str = "",
            ttl_s: float | None = None) -> None:
        while len(self._store) >= self._max:
            self._store.popitem(last=False)
            self._stats["evicted"] += 1
        self._store[key] = {
            "value": value,
            "source": source,
            "rev": self._revisions.get(source, 0),
            "expires_at": time.time() + float(ttl_s or self._ttl),
        }
        self._store.move_to_end(key)

    def invalidate(self, key: str) -> bool:
        if self._store.pop(key, None) is not None:
            self._stats["invalidated"] += 1
            return True
        return False

    def stats(self) -> dict[str, Any]:
        return {"ok": True, "entries": len(self._store),
                "capacity": self._max, **self._stats,
                "note": "cache is never account/holding/fill/authority"}
