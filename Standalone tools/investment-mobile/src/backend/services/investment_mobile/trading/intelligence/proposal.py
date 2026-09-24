"""ProposalFactory — structured TradeProposal for the governed pipeline.

Distinct from InvestmentRecommendation (user-facing advisory): a
proposal is the only object that may enter the strategy→risk→OMS
pipeline. Every proposal is safety-sanitized, schema-validated, expiry
bounded, and handed to the same gates as strategy-generated proposals.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from ..contracts import OrderSide, TradeProposal
from .safety import AISafetyBoundary


class ProposalFactory:
    def __init__(self, safety: AISafetyBoundary,
                 default_ttl_seconds: int = 3600) -> None:
        self._safety = safety
        self._ttl = int(default_ttl_seconds)
        self._proposals: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    def build(self, payload: dict[str, Any]) -> dict[str, Any]:
        clean = self._safety.sanitize_proposal(payload)
        if not clean.get("ok"):
            return clean
        p = clean["proposal"]
        iid = str(p.get("instrument_id") or "")
        side = str(p.get("side") or "")
        qty = Decimal(str(p.get("quantity") or 0))
        price = p.get("price")
        notional = Decimal(str(p.get("notional") or 0))
        if not iid or ":" not in iid:
            return {"ok": False, "error_code": "PROPOSAL_INSTRUMENT_INVALID"}
        if side not in {s.value for s in OrderSide}:
            return {"ok": False, "error_code": "PROPOSAL_SIDE_INVALID"}
        if qty <= 0 and notional <= 0:
            return {"ok": False, "error_code": "PROPOSAL_SIZE_INVALID"}
        if price is not None and Decimal(str(price)) <= 0:
            return {"ok": False, "error_code": "PROPOSAL_PRICE_INVALID"}
        ttl = int(p.get("expires_in_seconds") or self._ttl)
        proposal = TradeProposal(
            instrument_id=iid, market=str(p.get("market") or ""),
            side=side, quantity=float(qty),
            price=float(price) if price is not None else None,
            notional=float(notional),
            account_id=str(p.get("account_id") or ""),
            strategy_id=str(p.get("strategy_id") or "ai-proposal"),
            signal_id=str(p.get("signal_id") or ""),
            risk_params={
                "strategy_version": str(p.get("strategy_version") or ""),
                "evidence_refs": list(p.get("evidence_refs") or []),
                "expires_at": time.time() + ttl,
                "source": "ai-intelligence",
            },
        )
        self._proposals[proposal.proposal_id] = {
            "proposal": proposal.to_dict(),
            "status": "VALIDATED",
            "expires_at": time.time() + ttl,
        }
        return {"ok": True, "proposal": proposal.to_dict(),
                "status": "VALIDATED",
                "note": "仍需通過策略/持倉/資金/限制/風控/權限全鏈檢查"}

    def status(self, proposal_id: str) -> dict[str, Any]:
        row = self._proposals.get(proposal_id)
        if row is None:
            return {"ok": False, "error_code": "PROPOSAL_NOT_FOUND"}
        if (row["status"] == "VALIDATED"
                and time.time() > row["expires_at"]):
            row["status"] = "EXPIRED"
        return {"ok": True, **row}

    def get_valid(self, proposal_id: str) -> TradeProposal | None:
        row = self._proposals.get(proposal_id)
        if row is None:
            return None
        if row["status"] != "VALIDATED" or time.time() > row["expires_at"]:
            if row["status"] == "VALIDATED":
                row["status"] = "EXPIRED"
            return None
        p = row["proposal"]
        return TradeProposal(
            instrument_id=p["instrument_id"], market=p["market"],
            side=p["side"], quantity=p["quantity"], price=p.get("price"),
            notional=p.get("notional") or 0.0,
            account_id=p.get("account_id") or "",
            strategy_id=p.get("strategy_id") or "ai-proposal",
            signal_id=p.get("signal_id") or "",
            risk_params=dict(p.get("risk_params") or {}),
            proposal_id=p["proposal_id"], created_at=p["created_at"],
        )

    def mark(self, proposal_id: str, status: str) -> None:
        if proposal_id in self._proposals:
            self._proposals[proposal_id]["status"] = status
