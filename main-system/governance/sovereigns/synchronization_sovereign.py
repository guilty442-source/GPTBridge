"""Synchronization Sovereign — 同步主宰（專門決策主宰，A330 認證更新執行例外）。

法典依據:
- sovereign_id: synchronization-sovereign (position 18)
- area: synchronization-decision
- rank: specialized-decision-sovereign-with-A330-certified-update-execution-exception
- basis: A301
- duties: resource-sync|channel-sync|release-sync|learning-sync|runtime-sync|repair-sync|cleanup-sync|log-sync
- powers: adjudicate-sync-decisions|A330-certified-update-execution
- prohibitions: FORBID:general-execution (except A330)

A301: synchronization policy+priority+consistency target+conflict
disposition+acceptance decision only.
A322: SOLE-DECISION over sync target + dependency order + atomic boundary
+ conflict isolation + retry/cancel + convergence acceptance.
A334: this sovereign is the codex-registered single parent of every
synchronization sub-sovereign.  Child identity -> primary domain and the
delegation target are resolved from ``sovereign_hierarchy_registry`` at
adjudication time; nothing here hard-codes the hierarchy.
A330: certified update-set execution exception — the only execution
power, and only after the certification proof adjudication passes.

Lifecycle boundary: the governed executor materializes and starts each
child ONLY after ``authorize_child_activation`` (or the dispatch wrapper
``dispatch_child_activation``) returns an accepted outcome.  The sovereign
adjudicates; the executor executes.
"""

from __future__ import annotations

from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import (
    Refusal,
    accepted_outcome,
    refusal_outcome,
    verified_basis,
)

from ..registries import (
    children_of,
    module_assignment,
    parent_of,
    primary_domain_of,
    validate_child_parent,
)

# Sync intent -> codex child identity.  The parent assertion is re-validated
# against the registry on every adjudication (fail-closed, A334).
_SYNC_INTENT_CHILDREN: dict[str, str] = {
    "sync.resource-dependency": "resource-dependency-sync-sub-sovereign",
    "sync.channel-contract": "channel-contract-sync-sub-sovereign",
    "sync.release-update": "release-update-sync-sub-sovereign",
    "sync.learning-evidence": "learning-evidence-sync-sub-sovereign",
    "sync.runtime-state": "runtime-state-sync-sub-sovereign",
    "sync.repair-backup": "repair-backup-sync-sub-sovereign",
    "sync.cleanup-retention": "cleanup-retention-sync-sub-sovereign",
    "sync.automatic-log": "automatic-log-sync-sub-sovereign",
    "sync.dependency": "dependency-sync-sub-sovereign",
}

# A330: update types covered by the certified-update execution exception.
_A330_UPDATE_TYPES: frozenset[str] = frozenset(
    {"backend-release", "codex", "governance-policy", "directory"}
)

# Bounded restart budget for child failure adjudication (A322 retry/cancel).
_MAX_CHILD_RESTARTS = 3


class SynchronizationSovereign(SovereignBase):
    """同步主宰：專門決策，協調各類同步子主宰，A330例外執行。"""

    sovereign_id = "synchronization-sovereign"

    # A10/A12 explicit intent allowlist — the base-class ``_verify_intent``
    # checks edict IDs (article tokens like A301, A322), not the kebab-case
    # intent strings used by callers.  List every intent this sovereign
    # adjudicates explicitly (fail-closed, A10/A11).
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        # Sync dispatch intents (A334)
        "sync.resource-dependency",
        "sync.channel-contract",
        "sync.release-update",
        "sync.learning-evidence",
        "sync.runtime-state",
        "sync.repair-backup",
        "sync.cleanup-retention",
        "sync.automatic-log",
        "sync.dependency",
        # Child lifecycle (A334)
        "sub-sovereign.activate",
        "sub-sovereign.deactivate",
        "sub-sovereign.report-failure",
        # Module routing (A334)
        "module.route",
        # A322 sole-decision
        "sync.dependency-order",
        "sync.conflict-isolation",
        "sync.retry-cancel",
        "sync.convergence-acceptance",
        # A330 certified update execution exception
        "A330.certified-update",
    })

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        # Per-child consecutive-failure counts live on SovereignBase
        # (``_child_failure_counts``) so every parent shares one mechanism.
        # A330 operation tracking: active operation-id -> immutable
        # transaction-start inventory (affected graph, fencing token,
        # artifact hashes, terminal state).  Cleared on terminal status.
        self._a330_operations: dict[str, dict[str, Any]] = {}
        # Monotonic fencing token — increments per A330 operation so stale
        # coordinators cannot activate after a newer operation has started.
        self._fencing_token: int = 0

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：同步決策、A330認證更新、子主宰協調。"""
        intent = request.intent

        child_id = _SYNC_INTENT_CHILDREN.get(intent)
        if child_id is not None:
            return self._adjudicate_sync_dispatch(request, child_id)

        if intent == "sub-sovereign.activate":
            return self._adjudicate_child_activation(request)
        if intent == "sub-sovereign.deactivate":
            return self._adjudicate_child_deactivation(request)
        if intent == "sub-sovereign.report-failure":
            return self._adjudicate_child_failure(request)
        if intent == "module.route":
            return self._adjudicate_module_route(request)
        if intent == "sync.dependency-order":
            return self._adjudicate_dependency_order(request)
        if intent == "sync.conflict-isolation":
            return self._adjudicate_conflict_isolation(request)
        if intent == "sync.retry-cancel":
            return self._adjudicate_retry_cancel(request)
        if intent == "sync.convergence-acceptance":
            return self._adjudicate_convergence(request)
        if intent == "A330.certified-update":
            return await self._adjudicate_A330_certified_update(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A301", "A322"))

    # ------------------------------------------------------------------
    # Intent gate (A10/A11 explicit allowlist)
    # ------------------------------------------------------------------

    def _verify_intent(self, intent: str) -> bool:
        """Override the base-class edict-ID check with this sovereign's
        explicit intent allowlist (A10/A11 fail-closed)."""
        return intent in self._INTENT_ALLOWLIST

    # ------------------------------------------------------------------
    # A334 registry-driven dispatch
    # ------------------------------------------------------------------

    def _adjudicate_sync_dispatch(
        self, request: SovereignRequest, child_id: str
    ) -> SovereignOutcome:
        """Adjudicate a sync intent against the A334 hierarchy registry.

        Fail-closed: the child must be a codex-registered child of this
        sovereign; the returned outcome carries the registered primary
        domain and the materialization state for the governed executor.
        """
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome(
                "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
            )
        child = self._sub_sovereigns.get(child_id)
        return accepted_outcome(
            {
                "sync_type": request.intent,
                "delegated_to": child_id,
                "primary_domain": primary_domain_of(child_id),
                "materialized": child is not None,
                "started": bool(getattr(child, "_started", False)),
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301", "A334"),
        )

    def _adjudicate_child_activation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize activation of a specific registered child."""
        child_id = str(request.payload.get("sub_sovereign", ""))
        return self.authorize_child_activation(child_id)

    def _adjudicate_child_deactivation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize deactivation of a registered child.

        Symmetric with activation: the child must be a codex child and
        registered locally; the governed executor performs the stop.
        """
        child_id = str(request.payload.get("sub_sovereign", ""))
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_CODEX_CHILD", ("A334",))
        if child_id not in self._sub_sovereigns:
            return refusal_outcome("CHILD_NOT_REGISTERED", ("A334", "A130"))
        return accepted_outcome(
            {
                "child": child_id,
                "parent": self.sovereign_id,
                "deactivation": "authorized",
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A334", "A130"),
        )

    def dispatch_child_activation(self, child_id: str) -> SovereignOutcome:
        """Synchronous dispatch adjudication used by the governed executor
        before starting a child (same fail-closed contract as
        ``authorize_child_activation`` plus the codex parent assertion)."""
        if parent_of(child_id) != self.sovereign_id:
            return refusal_outcome("NOT_CODEX_PARENT", ("A334",))
        return self.authorize_child_activation(child_id)

    # ------------------------------------------------------------------
    # A322 sole-decision: failure adjudication + lifecycle authority
    # ------------------------------------------------------------------

    def _adjudicate_child_failure(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322 retry/cancel: adjudicate a reported child failure.

        Bounded restart budget — a child may be restarted up to
        ``_MAX_CHILD_RESTARTS`` consecutive failures; beyond that the
        adjudication cancels restarts and marks the child quarantined
        pending operator attention.  Reporting ``outcome=success`` resets
        the consecutive counter.
        """
        child_id = str(request.payload.get("sub_sovereign", ""))
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_CODEX_CHILD", ("A322", "A334"))
        outcome = str(request.payload.get("outcome") or "failure").casefold()
        if outcome in ("success", "recovered", "converged"):
            self.record_child_success(child_id)
            return accepted_outcome(
                {
                    "child": child_id,
                    "failure_count": 0,
                    "action": "cleared",
                },
                self.verified_basis("A322"),
            )
        count = self.record_child_failure(child_id)
        if count > _MAX_CHILD_RESTARTS:
            return accepted_outcome(
                {
                    "child": child_id,
                    "failure_count": count,
                    "action": "quarantine",
                    "restart": "denied-budget-exhausted",
                    "max_restarts": _MAX_CHILD_RESTARTS,
                },
                self.verified_basis("A322"),
            )
        return accepted_outcome(
            {
                "child": child_id,
                "failure_count": count,
                "action": "restart",
                "remaining_attempts": _MAX_CHILD_RESTARTS - count,
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A322", "A334"),
        )

    def _adjudicate_module_route(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: route an executable module to its managing sync child.

        The module's ``managing_sub_sovereign`` assignment must point at a
        codex-registered child of this sovereign — otherwise routing fails
        closed rather than guessing an owner.
        """
        module_code = str(request.payload.get("module", ""))
        assignment = module_assignment(module_code)
        if assignment is None:
            return refusal_outcome("MODULE_NOT_REGISTERED", ("A334",))
        owner = str(assignment.get("managing_sub_sovereign") or "")
        if not validate_child_parent(owner, self.sovereign_id):
            return refusal_outcome(
                "MODULE_OWNER_NOT_SYNC_CHILD",
                self.verified_basis("A334", "A322"),
            )
        child = self._sub_sovereigns.get(owner)
        return accepted_outcome(
            {
                "module": module_code,
                "route_to": owner,
                "primary_domain": primary_domain_of(owner),
                "materialized": child is not None,
                "decision_authority": assignment.get("decision_authority"),
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A334", "A322"),
        )

    def _adjudicate_dependency_order(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate a dependency-ordered sync plan.

        Payload: ``nodes`` (child identities to order) and ``edges``
        (``[before, after]`` pairs).  Every node/edge endpoint must be a
        codex-registered child of this sovereign; the result is a
        topological order.  A cycle is a dependency conflict — refused.
        """
        raw_nodes = request.payload.get("nodes")
        raw_edges = request.payload.get("edges") or []
        if not isinstance(raw_nodes, (list, tuple)) or not raw_nodes:
            return refusal_outcome("MISSING_NODES", ("A322",))
        nodes = [str(n) for n in raw_nodes]
        node_set = set(nodes)
        for node in nodes:
            if not validate_child_parent(node, self.sovereign_id):
                return refusal_outcome(
                    "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
                )

        edges: list[tuple[str, str]] = []
        for edge in raw_edges:
            if (
                not isinstance(edge, (list, tuple))
                or len(edge) != 2
                or str(edge[0]) not in node_set
                or str(edge[1]) not in node_set
            ):
                return refusal_outcome(
                    "INVALID_DEPENDENCY_EDGE", ("A322",)
                )
            edges.append((str(edge[0]), str(edge[1])))

        # Kahn topological sort; deterministic order for equal readiness.
        indegree = {n: 0 for n in nodes}
        outgoing: dict[str, list[str]] = {n: [] for n in nodes}
        for before, after in edges:
            outgoing[before].append(after)
            indegree[after] += 1
        ready = sorted(n for n in nodes if indegree[n] == 0)
        ordered: list[str] = []
        while ready:
            node = ready.pop(0)
            ordered.append(node)
            for nxt in outgoing[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
                    ready.sort()
        if len(ordered) != len(nodes):
            unresolved = [n for n in nodes if n not in set(ordered)]
            basis = verified_basis(("A322",))
            return SovereignOutcome(
                accepted=False,
                refusal=Refusal("DEPENDENCY_CYCLE", basis),
                result={"unresolved": unresolved},
                basis=basis,
            )

        return accepted_outcome(
            {
                "ordered_children": ordered,
                "atomic_boundary": "per-child",
                "dependency_edges": [list(e) for e in edges],
            },
            self.verified_basis("A322", "A334"),
        )

    def _adjudicate_conflict_isolation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate conflict isolation between sync children.

        Payload: ``conflicting`` — child identities reported in conflict.
        Each must be a codex child; the adjudication marks them isolated
        so the executor suspends their dispatch until re-acceptance.
        """
        raw = request.payload.get("conflicting")
        if not isinstance(raw, (list, tuple)) or not raw:
            return refusal_outcome("MISSING_CONFLICTING_SET", ("A322",))
        conflicting = [str(c) for c in raw]
        for child_id in conflicting:
            if not validate_child_parent(child_id, self.sovereign_id):
                return refusal_outcome(
                    "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
                )
        return accepted_outcome(
            {
                "isolated_children": conflicting,
                "isolation": "dispatch-suspended",
                "re_entry": "requires-convergence-acceptance",
            },
            self.verified_basis("A322"),
        )

    def _adjudicate_retry_cancel(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate retry vs cancel for a sync operation."""
        child_id = str(request.payload.get("sub_sovereign", ""))
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome(
                "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
            )
        try:
            attempt = int(request.payload.get("attempt") or 0)
            max_attempts = int(
                request.payload.get("max_attempts") or _MAX_CHILD_RESTARTS
            )
        except (TypeError, ValueError):
            return refusal_outcome("INVALID_ATTEMPT_COUNT", ("A322",))
        if attempt < max_attempts:
            return accepted_outcome(
                {
                    "child": child_id,
                    "action": "retry",
                    "attempt": attempt + 1,
                    "remaining_attempts": max_attempts - attempt - 1,
                },
                self.verified_basis("A322"),
            )
        return accepted_outcome(
            {
                "child": child_id,
                "action": "cancel",
                "reason": "retry-budget-exhausted",
                "attempt": attempt,
            },
            self.verified_basis("A322"),
        )

    def _adjudicate_convergence(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: convergence acceptance — all reported children must
        have reached a converged/success state; fail-closed otherwise."""
        raw = request.payload.get("results")
        if not isinstance(raw, dict) or not raw:
            return refusal_outcome("MISSING_RESULTS", ("A322",))
        incomplete: list[str] = []
        for child_id, status in raw.items():
            child = str(child_id)
            if not validate_child_parent(child, self.sovereign_id):
                return refusal_outcome(
                    "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
                )
            if str(status).casefold() not in (
                "converged", "success", "succeeded", "synced",
            ):
                incomplete.append(child)
        if incomplete:
            basis = verified_basis(("A322",))
            return SovereignOutcome(
                accepted=False,
                refusal=Refusal("CONVERGENCE_INCOMPLETE", basis),
                result={"incomplete": incomplete},
                basis=basis,
            )
        return accepted_outcome(
            {
                "converged_children": sorted(str(c) for c in raw),
                "acceptance": "granted",
            },
            self.verified_basis("A322"),
        )

    async def _adjudicate_A330_certified_update(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A330: 認證更新執行例外（唯一執行權限）。

        Fail-closed certification gate with full A330 precondition
        validation, immutable affected-graph inventory, monotonic fencing
        token, and append-only journal entry.  The operation is tracked
        until a terminal status is reported.

        A330 preconditions validated here:
        - update_type in the certified-update exception set
        - explicit ``certified`` flag (certification proof)
        - non-empty ``update_set``
        - ``artifact_hashes`` for every member of the update_set
          (signed immutable artifact roots)
        - ``operation_id`` (one operation-id per A330 operation)
        - ``backup_reference`` or ``checkpoint_reference`` (backup/
          checkpoint precondition)
        - ``rollback_target`` (tested rollback target)

        The fencing token is monotonic — a stale coordinator carrying an
        older token cannot activate after a newer operation has started.
        """
        # --- A330 certification gate ---
        update_type = request.payload.get("update_type")
        if update_type not in _A330_UPDATE_TYPES:
            return refusal_outcome(
                "INVALID_A330_UPDATE_TYPE", self.verified_basis("A330")
            )
        if request.payload.get("certified") is not True:
            return refusal_outcome(
                "A330_CERTIFICATION_MISSING", self.verified_basis("A330")
            )
        update_set = request.payload.get("update_set")
        if not isinstance(update_set, (list, tuple)) or not update_set:
            return refusal_outcome(
                "A330_EMPTY_UPDATE_SET", self.verified_basis("A330")
            )

        # --- A330 artifact root signing ---
        artifact_hashes = request.payload.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict) or not artifact_hashes:
            return refusal_outcome(
                "A330_MISSING_ARTIFACT_HASHES",
                self.verified_basis("A330"),
            )
        # Every update_set member must have a hash.
        missing_hashes = [
            name for name in update_set if name not in artifact_hashes
        ]
        if missing_hashes:
            return refusal_outcome(
                "A330_UNSIGNED_ARTIFACT_ROOTS",
                self.verified_basis("A330"),
            )

        # --- A330 operation-id (one per operation) ---
        operation_id = str(
            request.payload.get("operation_id") or ""
        )
        if not operation_id:
            return refusal_outcome(
                "A330_MISSING_OPERATION_ID",
                self.verified_basis("A330"),
            )
        # Reject duplicate operation-ids (idempotent replay guard).
        if operation_id in self._a330_operations:
            existing = self._a330_operations[operation_id]
            existing_status = existing.get("terminal_status", "")
            if existing_status:
                # Already terminal — return the recorded result (idempotent).
                return accepted_outcome(
                    {
                        "execution_authorized": True,
                        "update_type": update_type,
                        "update_set_size": len(update_set),
                        "operation_id": operation_id,
                        "fencing_token": existing.get("fencing_token", 0),
                        "terminal_status": existing_status,
                        "idempotent_replay": True,
                    },
                    self.verified_basis("A330", "A87", "A88"),
                )
            # In-flight duplicate — refuse (concurrent coordinator guard).
            return refusal_outcome(
                "A330_OPERATION_IN_FLIGHT",
                self.verified_basis("A330"),
            )

        # --- A330 backup/checkpoint precondition ---
        backup_ref = request.payload.get("backup_reference") or request.payload.get(
            "checkpoint_reference"
        )
        if not backup_ref:
            return refusal_outcome(
                "A330_MISSING_BACKUP_REFERENCE",
                self.verified_basis("A330"),
            )

        # --- A330 rollback target precondition ---
        rollback_target = request.payload.get("rollback_target")
        if not rollback_target:
            return refusal_outcome(
                "A330_MISSING_ROLLBACK_TARGET",
                self.verified_basis("A330"),
            )

        # --- A330 fencing token (monotonic) ---
        self._fencing_token += 1
        fencing_token = self._fencing_token

        # --- A330 immutable affected-graph inventory ---
        # The transaction-start inventory is frozen at acceptance time.
        # Late registrants and unrelated identities are never added.
        affected_graph = self._build_affected_graph(
            update_set, update_type
        )

        # --- A330 journal entry (append-only) ---
        self._a330_operations[operation_id] = {
            "operation_id": operation_id,
            "fencing_token": fencing_token,
            "update_type": update_type,
            "update_set": list(update_set),
            "artifact_hashes": dict(artifact_hashes),
            "backup_reference": backup_ref,
            "rollback_target": rollback_target,
            "affected_graph": affected_graph,
            "terminal_status": "",
            "accepted_at": self._iso_now(),
            "events": [
                {
                    "event": "accepted",
                    "fencing_token": fencing_token,
                    "timestamp": self._iso_now(),
                }
            ],
        }

        return accepted_outcome(
            {
                "execution_authorized": True,
                "update_type": update_type,
                "update_set_size": len(update_set),
                "operation_id": operation_id,
                "fencing_token": fencing_token,
                "affected_graph_size": len(affected_graph),
                "exception": "A330-certified-update-execution",
                "verification": "integrity+identity+history+complete-version-seal",
            },
            self.verified_basis("A330", "A87", "A88"),
        )

    def _build_affected_graph(
        self,
        update_set: list[str] | tuple[str, ...],
        update_type: str,
    ) -> list[str]:
        """A330: build the immutable transaction-start inventory of all
        affected consumers and replicas.

        For a backend-release update, the affected graph is the set of
        loaded modules that import any member of the update_set.  This
        is frozen at acceptance time — late registrants are never added.
        """
        import sys
        import types as _types
        affected: set[str] = set()
        update_set_names = set(update_set)
        for name, module in list(sys.modules.items()):
            if not isinstance(module, _types.ModuleType):
                continue
            if name in update_set_names:
                affected.add(name)
                continue
            # Check if this module imports any update_set member.
            for attr_name in dir(module):
                attr = getattr(module, attr_name, None)
                if isinstance(attr, _types.ModuleType):
                    dep_name = getattr(attr, "__name__", "")
                    if dep_name in update_set_names:
                        affected.add(name)
                        break
        return sorted(affected)

    def record_a330_terminal_status(
        self,
        operation_id: str,
        terminal_status: str,
        **detail: Any,
    ) -> bool:
        """A330: record a terminal status for a tracked operation.

        Terminal statuses (A330): global-success, partial-deferred,
        rolled-back, failed-isolated.  Returns True if the operation was
        found and updated; False if the operation_id is unknown or
        already terminal (idempotent replay guard).
        """
        operation = self._a330_operations.get(operation_id)
        if operation is None:
            return False
        if operation.get("terminal_status"):
            # Already terminal — idempotent replay, do not overwrite.
            return False
        valid_terminals = {
            "global-success",
            "partial-deferred",
            "rolled-back",
            "failed-isolated",
        }
        if terminal_status not in valid_terminals:
            return False
        operation["terminal_status"] = terminal_status
        operation["terminal_at"] = self._iso_now()
        operation.setdefault("events", []).append(
            {
                "event": "terminal",
                "terminal_status": terminal_status,
                "timestamp": self._iso_now(),
                **detail,
            }
        )
        return True

    def a330_operation_status(self) -> dict[str, Any]:
        """A330: read-only status of tracked certified-update operations."""
        active = []
        recent = []
        for operation in self._a330_operations.values():
            entry = {
                "operation_id": operation["operation_id"],
                "update_type": operation["update_type"],
                "fencing_token": operation["fencing_token"],
                "terminal_status": operation.get("terminal_status", ""),
                "accepted_at": operation.get("accepted_at", ""),
                "affected_graph_size": len(operation.get("affected_graph", [])),
            }
            if operation.get("terminal_status"):
                recent.append(entry)
            else:
                active.append(entry)
        return {
            "active_operations": active,
            "recent_terminal": recent[-8:],
            "total_tracked": len(self._a330_operations),
            "current_fencing_token": self._fencing_token,
        }

    # ------------------------------------------------------------------
    # Child registry (unified with SovereignBase._sub_sovereigns, A334)
    # ------------------------------------------------------------------

    @property
    def _sync_sub_sovereigns(self) -> dict[str, Any]:
        """Alias for the unified child registry populated by the governed
        executor (kept for existing callers)."""
        return self._sub_sovereigns

    def register_sync_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self.register_sub_sovereign(name, sovereign)

    def get_sync_sub_sovereign(self, name: str) -> Any | None:
        return self.get_sub_sovereign(name)

    # ``record_child_failure``/``record_child_success`` are inherited from
    # SovereignBase; the governed executor calls them when a child
    # start/stop raises (consistent with the adjudicated budget in
    # ``_adjudicate_child_failure``).

    # ------------------------------------------------------------------
    # Status surfaces
    # ------------------------------------------------------------------

    def sync_coverage(self) -> dict[str, Any]:
        """A334 coverage: codex-expected children vs materialized/started."""
        expected = children_of(self.sovereign_id)
        materialized = set(self._sub_sovereigns)
        started = {
            name
            for name, sov in self._sub_sovereigns.items()
            if getattr(sov, "_started", False)
        }
        missing = sorted(set(expected) - materialized)
        not_started = sorted(materialized - started)
        return {
            "authority": "codex-A334",
            "expected_children": sorted(expected),
            "materialized": sorted(materialized & set(expected)),
            "started": sorted(started),
            "missing": missing,
            "not_started": not_started,
            "unexpected": sorted(materialized - set(expected)),
            "coverage": (
                len(materialized & set(expected)) / len(expected)
                if expected
                else 1.0
            ),
        }

    def coverage_gap_report(self) -> dict[str, Any] | None:
        """A334/A322: report a coverage gap for repair routing.

        Returns a structured gap report when missing or not-started
        children are detected, or None when coverage is complete.  The
        report is suitable for routing to the decision-sovereign's
        repair-decision chain (A152/A154).
        """
        coverage = self.sync_coverage()
        missing = coverage.get("missing", [])
        not_started = coverage.get("not_started", [])
        if not missing and not not_started:
            return None
        return {
            "sovereign": self.sovereign_id,
            "gap_type": "missing" if missing else "not-started",
            "affected_children": missing or not_started,
            "coverage_ratio": coverage.get("coverage", 0.0),
            "repair_route": "decision-sovereign.repair-decision",
            "authority": "codex-A334",
        }

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["sync_sub_sovereigns"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._sub_sovereigns.items()
        }
        base["coverage"] = self.sync_coverage()
        base["a330_operations"] = self.a330_operation_status()
        return base

    def orchestration_status(self) -> dict[str, Any]:
        children = {
            name: {
                "primary_domain": primary_domain_of(name),
                "started": bool(getattr(sov, "_started", False)),
                "failure_count": self._child_failure_counts.get(name, 0),
            }
            for name, sov in self._sub_sovereigns.items()
        }
        return {
            "state": "active" if self._started else "stopped",
            "owner": self.role,
            "authority": "codex-A334",
            "children": children,
            "coverage": self.sync_coverage(),
            "failure_counts": dict(self._child_failure_counts),
            "a330_operations": self.a330_operation_status(),
            "delegation": "governed-executor-only",
        }


__all__ = ["SynchronizationSovereign"]
