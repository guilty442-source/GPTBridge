"""Control-plane proposals, capabilities, commands and persistence (A185 split).

Extracted from ``git_control_plane`` (source-size contract): the
``_Actions`` mixin owns action proposals (§173-175), the command API that
validates and records but never executes (§198), capability tokens
(§199-201), command correlation + transaction trace (§195-196), the
progress heartbeat + deadlock watchdog (§193-194) and persistence +
restart reconcile (§206-207).  State lives on the ``GitControlPlane``
instance; this module only contributes methods.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from .git_control_plane_types import (
    _PROPOSAL_SPECS,
    ActionProposal,
    CapabilityToken,
    EventType,
    GitEvent,
    GitSnapshot,
    Severity,
    StateKind,
)


class _Actions:
    """Proposals, capabilities, trace and persistence for the control plane."""

    # -- proposals (§173-175) -------------------------------------------------

    def build_action_proposals(
        self, snap: Optional[GitSnapshot] = None,
    ) -> list[ActionProposal]:
        snap = snap or self.snapshot(kind=StateKind.VERIFIED)
        proposals: list[ActionProposal] = []
        for code in snap.reason_codes:
            spec = _PROPOSAL_SPECS.get(code)
            if spec is None:
                continue
            proposals.append(self._new_proposal(
                snap, reason_code=code, **spec))
        for p in proposals:
            self._proposals[p.proposal_id] = p
            self.emit(GitEvent(
                EventType.PROPOSAL_CREATED, Severity.INFO,
                detail=f"{p.proposal_id}:{p.reason_code}",
            ))
        return proposals

    def _new_proposal(
        self, snap: GitSnapshot, *, reason_code: str, description: str,
        command_plan: list[str], risk_tier: int,
        affected_refs: Optional[list[str]] = None,
        affected_worktrees: Optional[list[str]] = None,
    ) -> ActionProposal:
        return ActionProposal(
            proposal_id=uuid.uuid4().hex[:16],
            created_at=self._now(),
            based_on_generation=snap.generation,
            reason_code=reason_code,
            description=description,
            command_plan=command_plan,
            risk_tier=risk_tier,
            required_approval=(
                "authority" if risk_tier >= 3
                else "confirmation" if risk_tier == 2 else "none"),
            affected_worktrees=affected_worktrees or [],
            affected_refs=affected_refs or [],
            expires_at=self._now() + self._proposal_ttl,
            tier_policy_version=self.policy_version(),
            tier_policy_digest=self.policy_digest(),
            hook_digest_set=self.hook_digest(),
        )

    def validate_proposal(
        self, proposal: ActionProposal, *, generation: Optional[int] = None,
        policy_digest: Optional[str] = None,
        hook_digest: Optional[str] = None,
    ) -> str:
        """Re-verify a proposal against current state before any use.

        Returns "OK" or a machine-readable rejection:
        EXPIRED / STALE_DECISION / POLICY_CHANGED / HOOK_CHANGED.
        """
        if self._now() > proposal.expires_at:
            return "EXPIRED"
        current_gen = generation if generation is not None else self._generation
        if proposal.based_on_generation != current_gen:
            return "STALE_DECISION"
        if (policy_digest if policy_digest is not None
                else self.policy_digest()) != proposal.tier_policy_digest:
            return "POLICY_CHANGED"
        if (hook_digest if hook_digest is not None
                else self.hook_digest()) != proposal.hook_digest_set:
            return "HOOK_CHANGED"
        return "OK"

    # -- command API (§198): validate + record, never execute ----------------

    def request_action(
        self,
        proposal: ActionProposal,
        *,
        capability: Optional[CapabilityToken] = None,
        legacy_env_approval: bool = False,
        actor: str = "unknown",
    ) -> dict[str, Any]:
        """Submit a proposal for governance — the control plane never
        executes Git commands (§162, §174, §198)."""
        verdict = self.validate_proposal(proposal)
        approval_path = (
            "LEGACY_APPROVAL_PATH" if legacy_env_approval else "proposal"
        )
        decision = {
            "snapshot_generation": proposal.based_on_generation,
            "reason_codes": [proposal.reason_code],
            "proposal": proposal.to_dict(),
            "approval": approval_path,
            "actor": actor,
            "validation": verdict,
            "execution_result": "NOT_EXECUTED",
        }
        if verdict != "OK":
            decision["status"] = f"REJECTED:{verdict}"
        elif capability is not None:
            consumed = self.consume_capability(capability, proposal)
            decision["status"] = (
                "PROPOSAL_AUTHORIZED" if consumed
                else "REJECTED:CAPABILITY_INVALID")
        elif proposal.is_tier3():
            # §174: never auto-execute, never auto-escalate.
            decision["status"] = "PROPOSAL_PENDING_AUTHORITY"
        else:
            decision["status"] = "PROPOSAL_PENDING_APPROVAL"
        self._decisions.append(decision)
        return decision

    def decision_log(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._decisions]

    # -- capabilities (§199-201) ----------------------------------------------

    def issue_capability(
        self, *, actor: str, operation: str, proposal: ActionProposal,
        worktree: str = "", ref: str = "", ttl: float = 300.0,
    ) -> CapabilityToken:
        token = CapabilityToken(
            token_id=uuid.uuid4().hex,
            actor=actor, operation=operation,
            repository=str(self.root), worktree=worktree, ref=ref,
            proposal_id=proposal.proposal_id,
            generation=proposal.based_on_generation,
            expires_at=self._now() + ttl,
            nonce=uuid.uuid4().hex,
        )
        self._capabilities[token.token_id] = token
        return token

    def consume_capability(
        self, token: CapabilityToken, proposal: ActionProposal,
        *, result: str = "consumed",
    ) -> bool:
        """Single-use: bound to actor+operation+proposal+generation (§200)."""
        stored = self._capabilities.get(token.token_id)
        if stored is None or stored.used_at:
            return False
        if self._now() > stored.expires_at:
            return False
        if (stored.proposal_id != proposal.proposal_id
                or stored.generation != proposal.based_on_generation
                or stored.actor != token.actor
                or stored.operation != token.operation):
            return False
        stored.used_at = self._now()
        stored.result = result
        return True

    # -- command correlation + transaction trace (§195-196) -------------------

    def new_command_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def record_command(
        self, *, command_id: str, command: str, task_id: str = "",
        worker_id: str = "", transaction_id: str = "",
        audit_sequence: int = 0,
    ) -> None:
        self._commands.append({
            "command_id": command_id, "command": command,
            "task_id": task_id, "worker_id": worker_id,
            "transaction_id": transaction_id,
            "audit_sequence": audit_sequence,
            "timestamp": self._now(),
        })

    def trace_transaction(self, transaction_id: str) -> dict[str, Any]:
        """§196 — full timeline: events + commands for one transaction."""
        return {
            "transaction_id": transaction_id,
            "events": [e.to_dict() for e in self.events(
                transaction_id=transaction_id)],
            "commands": [c for c in self._commands
                         if c["transaction_id"] == transaction_id],
        }

    # -- progress heartbeat + deadlock watchdog (§193-194) ---------------------

    def record_heartbeat(self, operation_id: str, phase: str) -> None:
        entry = self._heartbeats.setdefault(operation_id, {
            "operation_id": operation_id, "started_at": self._now(),
            "phases": [],
        })
        entry["phases"].append({"phase": phase, "at": self._now()})
        entry["last_seen"] = self._now()

    def deadlock_watchdog(self) -> list[dict[str, Any]]:
        """Detect suspected stalls only — never kills a process (§193)."""
        warnings: list[dict[str, Any]] = []
        locks = self.collect("locks").get("locks", [])
        for lock in locks:
            if lock.get("stale"):
                warnings.append({
                    "kind": "LOCK_STALL_WARNING",
                    "lock": lock["name"],
                    "age_seconds": lock["age_seconds"],
                })
                self.emit(GitEvent(
                    EventType.LOCK_STALL_WARNING, Severity.WARN,
                    detail=f"lock stall: {lock['name']}",
                ))
        return warnings

    # -- persistence + restart reconcile (§206-207) -----------------------------

    def _persist(self, snap: GitSnapshot) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_verified_snapshot": snap.to_dict(),
                "last_generation": snap.generation,
                "open_incidents": [
                    k for k, a in self._alerts.items()
                    if a["state"] == "ACTIVE"],
                "proposals": [p.to_dict() for p in self._proposals.values()],
            }
            tmp = self._state_dir / "state.json.tmp"
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._state_dir / "state.json")
        except OSError:
            pass

    def restart_reconcile(self) -> dict[str, Any]:
        """§207 — reload persisted state, then let verified Git truth win."""
        state_path = self._state_dir / "state.json"
        persisted: dict[str, Any] = {}
        try:
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            persisted = {}
        live = self.snapshot(kind=StateKind.VERIFIED)
        last = persisted.get("last_verified_snapshot") or {}
        diverged = bool(last) and (
            last.get("main_revision") != live.main_revision
            or persisted.get("last_generation") != live.generation
        )
        self._generation = max(self._generation,
                               int(persisted.get("last_generation", 0)))
        return {
            "persisted_found": bool(persisted),
            "status": "STALE_RECONCILED" if diverged else "RECONCILED",
            "verified_generation": live.generation,
        }


__all__ = ["_Actions"]
