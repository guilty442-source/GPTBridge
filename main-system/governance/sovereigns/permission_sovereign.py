"""Permission Sovereign — 權限主宰（獨立特權機構，不執行、不審議、不授權超出法典）。

法典依據:
- sovereign_id: permission-sovereign (position 4)
- area: permission
- rank: independent-privileged-institution-no-review-no-execution
- basis: codex
- duty: preserve-independent-two-key-permission-authorization-boundary
- power: issue-deny-renew-restrict-suspend-revoke-exact-permission-after-current-...-review
- prohibitions:
  - overstep-execution
  - exceed-codex
  - self-grant
  - delegate
  - inherit
  - privilege-expansion
  - proxy-permission-matters
  - module-self-issue-permission-id
  - hold-or-exercise-any-direct-execution-power

Edicts:
- E4: OWNER:permission-sovereign; SCOPE:all-permission-matters; ACTIONS:manage-issue-terminate-supervise; PERM-ID:sovereign-managed; EXEC:none; BASIS:codex
- E111: PERMISSION-SOVEREIGN:independent+special-status+not-subordinate; NAME+DUTIES+POWERS:unchanged; OTHER-SOVEREIGNS:request-only-via-information-layer; BASIS:codex-only

The implementation was merged from ``core_system.permission_sovereign`` so
the active path keeps its directory-driven authorization master-entry
surface while operating under the governance-layer sovereign identity.

This agent surfaces the directory and the Codex permission decision basis to
the sovereign at the decision level.  Any effective execution is delegated to
the governed-executor (DirectoryAuthority / Authentication); the sovereign
never executes in-process.
"""

from __future__ import annotations

from typing import Any, Optional

from governance_rule.permission_directory.code_rule_directory import code_rule_directory_snapshot
from governance_rule.execution.codex_official import official_self_declaration
from governance_rule.permission_directory.governance_policy import governance_policy_snapshot
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from ._delegation import record_delegation_outcome
from core_system.codex_decision import (
    accepted_outcome,
    decision_basis,
    refusal_outcome,
)
from core_system.versioning import refresh_version_cache, version_registry_status
from core_system.permission_automation import PermissionAutomationOrchestrator

from .parallel_adjudication_mixin import ParallelAdjudicationMixin
from .parallel_adjudication import AdjudicationTask


def _permission_sovereign():
    # A74/A435: read the permission-sovereign declaration through the
    # official entry (governance-codex://official) with a self-attested
    # single-use session, not a direct load_governance_codex() call.
    return official_self_declaration("permission-sovereign")


_PERMISSION_SOVEREIGN = _permission_sovereign()
if _PERMISSION_SOVEREIGN is None:
    raise RuntimeError("permission sovereign not found in Governance Codex")

PERMISSION_SOVEREIGN_RESPONSIBILITIES = _PERMISSION_SOVEREIGN.duties


def re_certify_permission_sovereign() -> None:
    """Reload the codex and update the permission sovereign authority."""

    global _PERMISSION_SOVEREIGN, PERMISSION_SOVEREIGN_RESPONSIBILITIES
    _PERMISSION_SOVEREIGN = _permission_sovereign()
    if _PERMISSION_SOVEREIGN is None:
        raise RuntimeError("permission sovereign not found in Governance Codex after re-certify")
    PERMISSION_SOVEREIGN_RESPONSIBILITIES = _PERMISSION_SOVEREIGN.duties
    PermissionSovereign.ROLE = _PERMISSION_SOVEREIGN.id
    refresh_version_cache()


class PermissionSovereign(
    ParallelAdjudicationMixin,
    SovereignBase,
):
    """權限主宰：權限事務的目錄驅動裁決與唯讀協調面。"""

    sovereign_id = "permission-sovereign"

    ROLE = _PERMISSION_SOVEREIGN.id

    # A10/A11 explicit intent allowlist
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        # Permission queries and termination (A436/A10/A22)
        "permission.query",
        "permission.terminate",
        "permission.renew",
        "permission.restrict",
        "permission.suspend",
        "permission.revoke",
        # Directory verification (A7)
        "directory.verify",
        # Identity verification (A39)
        "identity.verify",
        # Execution compliance supervision (A436)
        "permission.supervise",
        # Authorization routing (A10/E4)
        "permission.authorize",
    })

    def __init__(self, app: Any | None = None, governance: Any | None = None) -> None:
        super().__init__(app)
        self._governance_ref: Any = governance
        self._directory = None  # 由 governance 注入
        # A436 supervision: record of execution-compliance violations.
        self._compliance_violations: list[dict[str, Any]] = []
        # A10/A22: in-memory cache of issued permission grants, backed by
        # the append-only ledger (permission_grant_ledger) so grants
        # survive restarts.  The ledger is the source of truth.
        self._issued_grants: dict[str, dict[str, Any]] = self._load_grants_from_ledger()
        # Permission automation orchestrator
        self._automation: Optional[PermissionAutomationOrchestrator] = None

    def _governance(self) -> Any:
        """Owned by PermissionSovereign (mixin fallback lives on the base)."""
        if self._governance_ref is not None:
            return self._governance_ref
        return getattr(self.app, "governance", None)

    def _load_grants_from_ledger(self) -> dict[str, dict[str, Any]]:
        """Load the current status of all grants from the append-only ledger."""
        try:
            from core_system.permission_grant_ledger import PERMISSION_LEDGER_PATH
            import json
            if not PERMISSION_LEDGER_PATH.is_file():
                return {}
            grants: dict[str, dict[str, Any]] = {}
            with PERMISSION_LEDGER_PATH.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pid = entry.get("permission_id")
                    if pid:
                        grants[pid] = {
                            "status": entry.get("status", "unknown"),
                            "requester": entry.get("requester", ""),
                        }
            return grants
        except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
            return {}

    # ------------------------------------------------------------------
    # Intent gate (A10/A11 explicit allowlist)
    # ------------------------------------------------------------------

    def _verify_intent(self, intent: str) -> bool:
        """Override base-class edict-ID check with explicit intent allowlist (A10/A11 fail-closed)."""
        return intent in self._INTENT_ALLOWLIST

    # ------------------------------------------------------------------
    # Parallel adjudication entry point
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """並行裁決：權限查詢、目錄驗證、終止監督（不授權超出法典）。"""
        intent = request.intent

        # Group 1: Permission operations (independent, can run in parallel)
        permission_handlers = {
            "permission.query": self._adjudicate_permission_query,
            "permission.terminate": self._adjudicate_permission_terminate,
            "permission.renew": self._adjudicate_permission_renew,
            "permission.restrict": self._adjudicate_permission_restrict,
            "permission.suspend": self._adjudicate_permission_suspend,
            "permission.revoke": self._adjudicate_permission_revoke,
        }

        # Group 2: Verification operations (independent)
        verification_handlers = {
            "directory.verify": self._adjudicate_directory_verify,
            "identity.verify": self._adjudicate_identity_verify,
        }

        # Group 3: Supervision and authorization
        supervision_handlers = {
            "permission.authorize": self._adjudicate_permission_authorize,
            "permission.supervise": self._adjudicate_permission_supervise,
        }

        # Route to appropriate handler
        if intent in permission_handlers:
            return await permission_handlers[intent](request)
        if intent in verification_handlers:
            return await verification_handlers[intent](request)
        if intent in supervision_handlers:
            return await supervision_handlers[intent](request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A10", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """權限主宰委派執行（A446/A121）。

        This sovereign is decision-only (A127/E111).  Execution is delegated
        to DirectoryAuthority / Authentication / governed-executor.  This
        hook attests that the adjudication was a pure decision and records
        the delegation outcome in the audit ledger.
        """
        return self._attach_delegation_receipt(decision, request, "decision-only")

    # ------------------------------------------------------------------
    # Adjudication handlers (modular, one per intent)
    # ------------------------------------------------------------------

    async def _adjudicate_permission_query(self, request: SovereignRequest) -> SovereignOutcome:
        """Permission query adjudication."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", self.verified_basis("A436"))

        grant = self._issued_grants.get(permission_id)
        return accepted_outcome(
            {
                "permission_id": permission_id,
                "grant": grant,
                "directory": directory_authority_snapshot(),
            },
            self.verified_basis("A436", "A10", "A22"),
        )

    async def _adjudicate_permission_terminate(self, request: SovereignRequest) -> SovereignOutcome:
        """Permission termination adjudication."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", self.verified_basis("A436"))

        return accepted_outcome(
            {"permission_id": permission_id, "action": "terminate", "execution": "directory-authority"},
            self.verified_basis("A436", "A10", "A22"),
        )

    async def _adjudicate_permission_renew(self, request: SovereignRequest) -> SovereignOutcome:
        """Permission renewal adjudication."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", self.verified_basis("A436"))

        return accepted_outcome(
            {"permission_id": permission_id, "action": "renew", "execution": "directory-authority"},
            self.verified_basis("A436", "A10", "A22"),
        )

    async def _adjudicate_permission_restrict(self, request: SovereignRequest) -> SovereignOutcome:
        """Permission restriction adjudication."""
        permission_id = request.payload.get("permission_id")
        restrictions = request.payload.get("restrictions", {})
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", self.verified_basis("A436"))

        return accepted_outcome(
            {
                "permission_id": permission_id,
                "action": "restrict",
                "restrictions": restrictions,
                "execution": "directory-authority",
            },
            self.verified_basis("A436", "A10", "A22"),
        )

    async def _adjudicate_permission_suspend(self, request: SovereignRequest) -> SovereignOutcome:
        """Permission suspension adjudication."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", self.verified_basis("A436"))

        return accepted_outcome(
            {"permission_id": permission_id, "action": "suspend", "execution": "directory-authority"},
            self.verified_basis("A436", "A10", "A22"),
        )

    async def _adjudicate_permission_revoke(self, request: SovereignRequest) -> SovereignOutcome:
        """Permission revocation adjudication."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", self.verified_basis("A436"))

        return accepted_outcome(
            {"permission_id": permission_id, "action": "revoke", "execution": "directory-authority"},
            self.verified_basis("A436", "A10", "A22"),
        )

    async def _adjudicate_directory_verify(self, request: SovereignRequest) -> SovereignOutcome:
        """Directory verification adjudication (A7)."""
        target = request.payload.get("target")
        return accepted_outcome(
            {
                "target": target,
                "directory_snapshot": directory_authority_snapshot(),
                "code_rule_snapshot": code_rule_directory_snapshot(),
            },
            self.verified_basis("A7", "A10"),
        )

    async def _adjudicate_identity_verify(self, request: SovereignRequest) -> SovereignOutcome:
        """Identity verification adjudication (A39)."""
        identity = request.payload.get("identity")
        return accepted_outcome(
            {
                "identity": identity,
                "identity_group_snapshot": identity_group_snapshot(),
                "identity_permission_snapshot": identity_permission_snapshot(),
            },
            self.verified_basis("A39", "A10"),
        )

    async def _adjudicate_permission_authorize(self, request: SovereignRequest) -> SovereignOutcome:
        """Permission authorization adjudication (A10/E4)."""
        permission_id = request.payload.get("permission_id")
        requester = request.requester
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", self.verified_basis("A10", "E4"))

        return accepted_outcome(
            {
                "permission_id": permission_id,
                "requester": requester,
                "authorization": "granted",
                "basis": "codex-only",
                "execution": "directory-authority",
            },
            self.verified_basis("A10", "E4", "A436"),
        )

    async def _adjudicate_permission_supervise(self, request: SovereignRequest) -> SovereignOutcome:
        """Execution compliance supervision adjudication (A436)."""
        violation = request.payload.get("violation")
        if not violation:
            return refusal_outcome("MISSING_VIOLATION", self.verified_basis("A436"))

        self._compliance_violations.append({
            "violation": violation,
            "requester": request.requester,
            "timestamp": _iso_now(),
        })

        return accepted_outcome(
            {"violation_recorded": True, "total_violations": len(self._compliance_violations)},
            self.verified_basis("A436", "A10"),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """啟動權限主宰（決策層初始化，A297）。

        Decision-layer initialization only: the automation loop is started
        separately by ``start_supervision()``.
        """
        state = await super().start()
        return state

    async def start_supervision(self) -> None:
        """Start the permission automation loop (A297 separation)."""
        await self.start_automation()

    async def stop_supervision(self) -> None:
        """Stop the permission automation loop (A297 separation)."""
        await self.stop_automation()

    async def stop(self) -> None:
        """停止權限主宰與自動化組件。"""
        await self.stop_automation()
        await super().stop()

    # ------------------------------------------------------------------
    # Automation (delegated to PermissionAutomationOrchestrator)
    # ------------------------------------------------------------------

    async def start_automation(self) -> None:
        """Start the permission automation orchestrator."""
        if self._automation is None:
            self._automation = PermissionAutomationOrchestrator(self.app)
        await self._automation.start()

    async def stop_automation(self) -> None:
        """Stop the permission automation orchestrator."""
        if self._automation is not None:
            await self._automation.stop()
            self._automation = None

    def re_certify(self) -> None:
        """Re-certify the permission sovereign after a codex amendment."""
        re_certify_permission_sovereign()
        governance = self._governance()
        if governance is not None and hasattr(governance, "re_certify"):
            governance.re_certify()


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


__all__ = ["PERMISSION_SOVEREIGN_RESPONSIBILITIES", "PermissionSovereign", "re_certify_permission_sovereign"]