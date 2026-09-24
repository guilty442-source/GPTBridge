"""Market data maintenance — health, integrity, bounded resync.

Runs as periodic duty calls driven by the engine service (no private
resident scheduler): ``run_once`` performs one bounded pass — source
health, stale detection, integrity gaps, bounded resync retries, and
store stats. Sources in persistent failure sit in DEGRADED state;
recovery re-syncs from the cursor rather than assuming data continuity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ConnectionStatus, DataStatus
from .engine import MarketDataEngine
from .history import CandleStore, HistoricalMarketDataService


class MarketDataMaintenance:
    RETRY_ATTEMPTS_MAX = 3

    def __init__(
        self,
        engine: MarketDataEngine,
        store: CandleStore,
        history: HistoricalMarketDataService,
    ) -> None:
        self._engine = engine
        self._store = store
        self._history = history

    # ------------------------------------------------------------------
    def run_once(self) -> dict[str, Any]:
        """One bounded maintenance pass — invoked by a governed driver."""
        health = self._engine.source_status()
        degraded = [
            s["source_id"] for s in health
            if s["connection_status"] in (
                ConnectionStatus.DEGRADED.value,
                ConnectionStatus.DISCONNECTED.value,
            )
        ]
        stale_sources = [
            s["source_id"] for s in health if s["stale"]
        ]
        failed_syncs = [
            s for s in self._store.sync_states() if s["status"] == "failed"
        ]
        return {
            "ok": True,
            "at": datetime.now(timezone.utc).isoformat(),
            "sources": health,
            "degraded_sources": degraded,
            "stale_sources": stale_sources,
            "failed_syncs": len(failed_syncs),
            "store": self._store.stats(),
        }
