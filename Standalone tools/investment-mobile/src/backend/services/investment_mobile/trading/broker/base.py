"""Broker adapter base — independent adapter architecture.

Verification contract: an adapter may only dispatch real orders when a
verification artifact ``runtime/state/broker-api/<broker_id>.verified.json``
exists, recording *which official API* was verified, *when*, and *by whom*.
The artifact is a recorded fact — adapters never self-certify.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..contracts import Order, OrderIntent


class BrokerAdapter:
    """Base class for broker connectivity."""

    broker_id = "abstract"
    market = ""
    label = ""

    def __init__(self, state_dir: Path) -> None:
        self._verify_path = (
            state_dir / "broker-api" / f"{self.broker_id}.verified.json"
        )

    # ------------------------------------------------------------------
    def verification(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._verify_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if not data.get("api") or not data.get("verified_by"):
            return None
        return data

    @property
    def api_verified(self) -> bool:
        return self.verification() is not None

    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        verification = self.verification()
        return {
            "broker_id": self.broker_id,
            "market": self.market,
            "label": self.label,
            "api_verified": bool(verification),
            "verified_api": (verification or {}).get("api"),
            "trading_enabled": self.api_verified,
            "capabilities": self.capabilities(),
        }

    def capabilities(self) -> dict[str, bool]:
        """What this adapter can do *right now* — analysis/simulation is
        always on; execution requires verification."""
        return {
            "analysis": True,
            "simulation": True,
            "quotes": False,
            "execution": self.api_verified,
        }

    # ------------------------------------------------------------------
    def place_order(self, order: Order, intent: OrderIntent) -> dict[str, Any]:
        if not self.api_verified:
            return {
                "ok": False,
                "error_code": "BROKER_API_UNVERIFIED",
                "broker_id": self.broker_id,
                "detail": (
                    "official trading API not verified; analysis and "
                    "simulated trading only"
                ),
            }
        return self._place_verified(order, intent)

    def _place_verified(self, order: Order, intent: OrderIntent) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "BROKER_NOT_CONNECTED",
            "broker_id": self.broker_id,
        }

    def get_quote(self, instrument: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "BROKER_NOT_CONNECTED",
            "broker_id": self.broker_id,
            "instrument": instrument,
        }

    def get_positions(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "BROKER_NOT_CONNECTED",
            "broker_id": self.broker_id,
        }

    def heartbeat(self) -> dict[str, Any]:
        return {
            "broker_id": self.broker_id,
            "connected": False,
            "api_verified": self.api_verified,
            "at": time.time(),
        }


class BrokerRegistry:
    """Market → adapter routing. Mutual-fund platforms are extensible."""

    def __init__(self, state_dir: Path) -> None:
        self._adapters: dict[str, BrokerAdapter] = {}
        self._by_market: dict[str, str] = {}
        self._state_dir = state_dir

    def register(self, adapter: BrokerAdapter, *, market: str) -> None:
        self._adapters[adapter.broker_id] = adapter
        self._by_market.setdefault(str(market), adapter.broker_id)

    def for_market(self, market: str) -> BrokerAdapter | None:
        broker_id = self._by_market.get(str(market))
        return self._adapters.get(broker_id) if broker_id else None

    def get(self, broker_id: str) -> BrokerAdapter | None:
        return self._adapters.get(str(broker_id))

    def status(self) -> list[dict[str, Any]]:
        return [a.status() for a in self._adapters.values()]


broker_registry: BrokerRegistry | None = None
