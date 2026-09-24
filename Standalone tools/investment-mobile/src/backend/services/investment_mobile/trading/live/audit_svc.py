"""TradingAuditService — correlated, masked, append-only audit.

Every answerable question about a trade resolves through
``correlation_id`` chains: proposal → authorization → risk decision →
order → submission → broker report → execution. Sensitive material is
masked before it ever reaches the journal.
"""

from __future__ import annotations

from typing import Any

from .contracts import AuditEvent, mask_sensitive


class TradingAuditService:
    def __init__(self, journal) -> None:
        self._journal = journal          # persistence.record

    def record(self, event_type: str, *,
               correlation_id: str = "", result: str = "",
               account_id: str = "", proposal_id: str = "",
               order_id: str = "", strategy_version: str = "",
               authorization_id: str = "", risk_decision_id: str = "",
               detail: dict[str, Any] | None = None) -> dict[str, Any]:
        ev = AuditEvent(
            event_type=str(event_type),
            correlation_id=str(correlation_id),
            account_id=str(account_id),
            proposal_id=str(proposal_id),
            order_id=str(order_id),
            strategy_version=str(strategy_version),
            authorization_id=str(authorization_id),
            risk_decision_id=str(risk_decision_id),
            result=str(result),
            detail=mask_sensitive(dict(detail or {})),
        )
        row = ev.to_dict()
        self._journal("live_audit", row)
        return row
