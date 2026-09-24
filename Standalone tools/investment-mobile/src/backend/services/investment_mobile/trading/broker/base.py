"""broker domain — adapter boundary.

Only the OMS may call ``place_order`` — 星澄/AI cannot reach adapters
(direct invocation is structurally impossible: adapters are constructed
inside the OMS boundary and accept ``OrderRequest`` objects, which only
the OMS creates after a positive ``RiskDecision``).

An adapter is inert until ``api_verified`` is true — set only by an
external verification artifact ``runtime/state/broker-api/<id>.verified.json``
recording which official API was validated, when, and by whom. The
adapter never self-certifies.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..contracts import Broker, OrderReceipt, OrderRequest, OrderStatus


class BrokerAdapter:
    """Per-broker adapter base — inert until verified."""

    broker_id: str = ""
    market: str = ""
    label: str = ""

    def __init__(self, state_dir: Path) -> None:
        self._verified_path = state_dir / "broker-api" / f"{self.broker_id}.verified.json"

    # ------------------------------------------------------------------
    @property
    def api_verified(self) -> bool:
        try:
            data = json.loads(self._verified_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        if not data.get("api") or not data.get("verified_by"):
            return False
        expires = float(data.get("expires_at") or 0)
        return expires > time.time() if expires else True

    def broker(self) -> Broker:
        return Broker(
            broker_id=self.broker_id,
            market=self.market,
            label=self.label,
            api_verified=self.api_verified,
        )

    # ------------------------------------------------------------------
    def place_order(self, order: OrderRequest) -> OrderReceipt:
        """OMS-only entry. Unverified adapters always deny — fail closed."""
        if not self.api_verified:
            return OrderReceipt(
                order_id=order.order_id,
                status=OrderStatus.ADAPTER_DENIED.value,
                rejection="BROKER_API_UNVERIFIED",
                simulated=False,
            )
        return self._dispatch(order)

    def _dispatch(self, order: OrderRequest) -> OrderReceipt:
        """Subclasses implement the real API call once verified."""
        return OrderReceipt(
            order_id=order.order_id,
            status=OrderStatus.ADAPTER_DENIED.value,
            rejection="ADAPTER_NOT_IMPLEMENTED",
            simulated=False,
        )

    # ------------------------------------------------------------------
    def supports(self, market: str) -> bool:
        return self.market == market


class BrokerRegistry:
    def __init__(self, state_dir: Path) -> None:
        from .cathay_tw import CathayTwAdapter
        from .fubon_us import FubonUsAdapter
        from .fund_platform import FundPlatformAdapter

        self._adapters: dict[str, BrokerAdapter] = {
            a.broker_id: a
            for a in (
                CathayTwAdapter(state_dir),
                FubonUsAdapter(state_dir),
                FundPlatformAdapter(state_dir),
            )
        }

    def adapter_for(self, broker_id: str) -> BrokerAdapter | None:
        return self._adapters.get(str(broker_id))

    def list_brokers(self) -> list[dict[str, Any]]:
        return [a.broker().to_dict() for a in self._adapters.values()]
