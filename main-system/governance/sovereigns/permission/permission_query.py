"""Permission Sovereign — Permission Query Adjudication (A436/A10/A22).

Read-only directory surface using the sealed DirectoryAuthoritySnapshot
and AccessGateway — the sovereign never calls nonexistent governance
methods like ``evaluate_permission``; it resolves permission decisions
from the directory-driven authority snapshots directly.
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome, verified_basis
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)


class PermissionQueryMixin:
    """Permission query and directory/identity verification."""

    app: Any
    _governance_ref: Any

    async def _adjudicate_permission_query(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A436/A10/A22: permission query — read-only directory surface."""
        actor = request.payload.get("actor") or request.requester
        capability = request.payload.get("capability")
        target = request.payload.get("target")
        if not capability or not target:
            return refusal_outcome(
                "INSUFFICIENT_QUERY_PARAMS", verified_basis(("A436", "A10"))
            )

        # Resolve from the sealed directory authority snapshot (read-only).
        permissions = identity_permission_snapshot()
        allowed = any(
            binding.actor == actor and capability in binding.capabilities
            for binding in permissions
        )
        basis_text = "directory-authority" if allowed else "default-deny"
        return accepted_outcome(
            {
                "query": "permission.query",
                "decision": "allowed" if allowed else "denied",
                "basis": basis_text,
                "source": "identity-permission-snapshot",
            },
            verified_basis(("A436", "A10", "A22")),
        )

    async def _adjudicate_directory_verify(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A7: directory verification — read-only snapshot surface."""
        authority = directory_authority_snapshot()
        return accepted_outcome(
            {
                "query": "directory.verify",
                "result": {
                    "authority_version": authority.authority_version,
                    "sealed": True,
                },
            },
            verified_basis(("A7",)),
        )

    async def _adjudicate_identity_verify(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A39: identity verification — read-only snapshot surface."""
        actor = request.payload.get("actor") or request.requester
        groups = identity_group_snapshot()
        identity = next(
            (item for item in groups.identities if item.bound_tool_id == actor),
            None,
        )
        verified = identity is not None
        return accepted_outcome(
            {
                "query": "identity.verify",
                "result": {
                    "actor": actor,
                    "verified": verified,
                    "bound_tool_id": identity.bound_tool_id if verified else None,
                },
            },
            verified_basis(("A39",)),
        )
