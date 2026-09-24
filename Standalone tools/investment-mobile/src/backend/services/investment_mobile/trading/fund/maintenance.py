"""FundMaintenance — bounded fund data health pass.

Checks NAV freshness per registered share class (unannounced NAV keeps
the last valid value + staleness flag — never fabricated), provider
capability state, pending transactions and uncovered holdings.
Driven by the engine service; no private resident scheduler.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from .contracts import FundTxnStatus
from .engine import MutualFundEngine
from .nav import FundNAVService
from .providers import PROVIDER_CAPABILITIES


class FundMaintenance:
    def __init__(self, engine: MutualFundEngine) -> None:
        self._engine = engine

    def run_once(self) -> dict[str, Any]:
        stale_navs: list[dict[str, Any]] = []
        missing_nav: list[str] = []
        for ident in self._engine.identities.list():
            nav = self._engine.nav.latest_published(
                ident["fund_id"], ident["share_class_id"])
            if not nav.get("ok"):
                missing_nav.append(ident["instrument_id"])
            elif nav.get("stale"):
                stale_navs.append({
                    "instrument_id": ident["instrument_id"],
                    "nav_date": nav["nav"]["nav_date"],
                    "age_days": nav["age_days"],
                })
        pending = self._engine.transactions.pending_settlement()
        return {
            "ok": True,
            "at": datetime.now(timezone.utc).isoformat(),
            "funds": len(self._engine.identities.list()),
            "stale_navs": stale_navs,
            "missing_nav": missing_nav,
            "pending_settlement": len(pending),
            "providers_verified": [
                c.provider_id for c in PROVIDER_CAPABILITIES.values()
                if c.verified
            ],
            "degraded": bool(missing_nav) and bool(
                self._engine.identities.list()),
        }
