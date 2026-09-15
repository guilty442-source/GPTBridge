"""Sovereign requester verification helpers (A10/A11/A116/A121/A435).

Extracted from ``SovereignBase`` to satisfy A430 class-size limits while
keeping the fail-closed identity-attestation contract in one place.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ._delegation import consume_delegation


# Governed in-process actors that may call sovereigns without a capability
# token (A121).  They carry their own governed execution path and never
# claim a sovereign identity.  Any requester string not in this allowlist,
# not self-adjudication, not a valid delegation nonce, and not a verified
# capability token is rejected (fail-closed).
_GOVERNED_IN_PROCESS_ACTORS: frozenset[str] = frozenset({
    "startup-executor",
    "permission-automation",
    "system-automation-coordinator",
    "governance-coordinator",
    "governed-executor",
    "decision-layer",
})


async def verify_token_requester(
    sovereign: Any, request: Any, token: Any
) -> bool:
    """Verify a capability-token-bearing requester (A10/A11/A116)."""
    if not isinstance(token, str) or not token:
        return False
    claims = await sovereign._authenticate_token_claims(request, token)
    if claims is None:
        return False
    request.payload["_verified_claims"] = {
        "actor": claims.actor,
        "bound_tool_id": claims.bound_tool_id,
        "identity_group": claims.identity_group,
        "capability": claims.capability,
        "action": claims.action,
        "target": claims.target,
    }
    return True


def verify_delegation_nonce(
    sovereign: Any, request: Any, nonce: str
) -> bool:
    """Verify a single-use delegation nonce (A435 single-use)."""
    if not consume_delegation(
        nonce,
        parent=request.requester,
        child=sovereign.sovereign_id,
        intent=request.intent,
    ):
        return False
    request.payload["_verified_delegation"] = {
        "parent": request.requester,
        "child": sovereign.sovereign_id,
        "intent": request.intent,
    }
    return True


async def verify_requester(sovereign: Any, request: Any) -> bool:
    """Verify a requester's identity (A10/A11/A116/A121/A435 fail-closed).

    Identity proofs accepted, fail-closed:

    - ``capability_token`` present → it MUST verify through the governance
      authentication service, its ``actor`` claim must equal
      ``request.requester`` and its capability must cover the request.
    - ``_delegation_nonce`` present → it MUST be an unconsumed, unexpired
      single-use delegation session minted by another sovereign for this
      sovereign and intent (A435 single-use; replayed/forged denies).
    - self-adjudication (``requester == sovereign_id``) is accepted.
    - a sovereign-identity claim without a token or a valid single-use
      delegation session is rejected — a bare string is unverifiable.
    - other governed in-process actors keep their governed path; they
      never claim a sovereign identity.
    """
    if not isinstance(request.requester, str) or not request.requester:
        return False
    # Proof stamps are set ONLY by the verification layer; a caller may not
    # present a pre-stamped ``_verified_delegation``/``_verified_claims``
    # (forgery surface — A121/A435 fail-closed).
    request.payload.pop("_verified_delegation", None)
    request.payload.pop("_verified_claims", None)
    token = request.payload.get("capability_token")
    if token is not None:
        return await verify_token_requester(sovereign, request, token)
    nonce = request.payload.get("_delegation_nonce")
    if isinstance(nonce, str) and nonce:
        return verify_delegation_nonce(sovereign, request, nonce)
    if request.requester == sovereign.sovereign_id:
        return True
    if sovereign._claims_sovereign_identity(request.requester):
        return False
    return request.requester in _GOVERNED_IN_PROCESS_ACTORS


__all__ = [
    "_GOVERNED_IN_PROCESS_ACTORS",
    "verify_delegation_nonce",
    "verify_requester",
    "verify_token_requester",
]
