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
from core_system.codex_decision import (
    accepted_outcome,
    decision_basis,
    refusal_outcome,
)
from core_system.versioning import refresh_version_cache, version_registry_status
from core_system.permission_automation import PermissionAutomationOrchestrator

# Sub-modules
from .permission.permission_query import PermissionQueryMixin
from .permission.permission_lifecycle import PermissionLifecycleMixin
from .permission.auth_supervision import PermissionAuthSupervisionMixin
from .permission.automation import PermissionAutomationMixin
from .permission.status import PermissionStatusMixin


def _permission_sovereign():
    # A74/A174: read the permission-sovereign declaration through the
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
    PermissionQueryMixin,
    PermissionLifecycleMixin,
    PermissionAuthSupervisionMixin,
    PermissionAutomationMixin,
    PermissionStatusMixin,
    SovereignBase,
):
    """權限主宰：權限事務的目錄驅動裁決與唯讀協調面。"""

    sovereign_id = "permission-sovereign"

    ROLE = _PERMISSION_SOVEREIGN.id

    # A10/A11 explicit intent allowlist
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        # Permission queries and termination (A6/A10/A22)
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
        # Execution compliance supervision (A6)
        "permission.supervise",
        # Authorization routing (A10/E4)
        "permission.authorize",
    })

    def __init__(self, app: Any | None = None, governance: Any | None = None) -> None:
        super().__init__(app)
        self._governance_ref: Any = governance
        self._directory = None  # 由 governance 注入
        # A6 supervision: record of execution-compliance violations.
        self._compliance_violations: list[dict[str, Any]] = []
        # A10/A22: in-memory cache of issued permission grants, backed by
        # the append-only ledger (permission_grant_ledger) so grants
        # survive restarts.  The ledger is the source of truth.
        self._issued_grants: dict[str, dict[str, Any]] = self._load_grants_from_ledger()
        # Permission automation orchestrator
        self._automation: Optional[PermissionAutomationOrchestrator] = None

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
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Single-gate adjudication (A10/A11)
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：權限查詢、目錄驗證、終止監督（不授權超出法典）。"""
        intent = request.intent

        if intent == "permission.query":
            return await self._adjudicate_permission_query(request)
        if intent == "permission.terminate":
            return await self._adjudicate_permission_terminate(request)
        if intent == "permission.renew":
            return await self._adjudicate_permission_renew(request)
        if intent == "permission.restrict":
            return await self._adjudicate_permission_restrict(request)
        if intent == "permission.suspend":
            return await self._adjudicate_permission_suspend(request)
        if intent == "permission.revoke":
            return await self._adjudicate_permission_revoke(request)
        if intent == "directory.verify":
            return await self._adjudicate_directory_verify(request)
        if intent == "identity.verify":
            return await self._adjudicate_identity_verify(request)
        if intent == "permission.authorize":
            return await self._adjudicate_permission_authorize(request)
        if intent == "permission.supervise":
            return await self._adjudicate_permission_supervise(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A10", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """權限主宰委派執行（A69/A121）。

        This sovereign is decision-only (A127/E111).  Execution is delegated
        to DirectoryAuthority / Authentication / governed-executor.
        """
        return decision

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """啟動權限主宰與自動化組件。"""
        state = await super().start()
        await self.start_automation()
        state["automation"] = "started"
        return state

    async def stop(self) -> None:
        """停止權限主宰與自動化組件。"""
        await self.stop_automation()
        await super().stop()

    def re_certify(self) -> None:
        """Re-certify the permission sovereign after a codex amendment."""
        re_certify_permission_sovereign()
        governance = self._governance()
        if governance is not None and hasattr(governance, "re_certify"):
            governance.re_certify()

    def _governance(self) -> Any:
        if self._governance_ref is not None:
            return self._governance_ref
        return getattr(self.app, "governance", None)

    def _iso_now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


__all__ = ["PERMISSION_SOVEREIGN_RESPONSIBILITIES", "PermissionSovereign", "re_certify_permission_sovereign"]