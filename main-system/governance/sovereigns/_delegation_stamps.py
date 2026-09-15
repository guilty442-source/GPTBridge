"""Delegation stamping helpers — split from _delegation.py (A185)."""

from __future__ import annotations

from typing import Any

def _stamp_delegation(
    request: Any,
    sovereign_id: str,
    target_sovereign_id: str,
) -> Any:
    """Stamp a single-use delegation nonce onto the forwarded request."""
    from core_system.codex_decision import SovereignRequest

    payload = dict(request.payload)
    payload["_delegated_by"] = sovereign_id
    payload["_delegation_nonce"] = _mint_delegation(
        sovereign_id, target_sovereign_id, request.intent
    )
    return SovereignRequest(
        intent=request.intent,
        subject=request.subject,
        requester=sovereign_id,
        payload=payload,
    )


def _attach_target_receipt(
    outcome: Any,
    request: Any,
    sovereign_id: str,
    target_sovereign_id: str,
) -> Any:
    """Attach a verifiable delegation receipt carrying the target's trail."""
    from core_system.codex_decision import SovereignOutcome

    target_receipts: tuple[dict[str, Any], ...] = ()
    target_result = outcome.result or {}
    if isinstance(target_result, dict):
        summary = target_result.get("execution_receipts")
        if isinstance(summary, dict):
            target_receipts = tuple(summary.get("tiers", ()))
    receipt = _mint_delegation_receipt(
        sovereign_id=sovereign_id,
        intent=request.intent,
        requester=request.requester,
        accepted=outcome.accepted,
        reason_code=outcome.refusal.reason_code if outcome.refusal else "",
        execution_mode="delegated-to-target",
        basis=outcome.basis,
        target_sovereign=target_sovereign_id,
        target_receipts=target_receipts,
    )
    result = dict(outcome.result or {})
    result["delegation_receipt"] = receipt.to_dict()
    return SovereignOutcome(
        accepted=outcome.accepted,
        refusal=outcome.refusal,
        result=result,
        basis=outcome.basis,
    )

def _mint_delegation(*args: Any, **kwargs: Any) -> Any:
    from ._delegation import mint_delegation

    return mint_delegation(*args, **kwargs)


def _mint_delegation_receipt(*args: Any, **kwargs: Any) -> Any:
    from ._delegation import mint_delegation_receipt

    return mint_delegation_receipt(*args, **kwargs)
