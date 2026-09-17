"""Capability gate adapter for governed Git execution.

``execute_with_capability()`` is the migration surface described by the
A390-428 core: callers that already own a tier decision attach a capability
token; the gate normalizes the command, verifies the token in the documented
order, consumes it through the single-use ledger, and only then delegates to
the existing ``GitRepository`` gate.  It is an adapter, not a replacement:
callers that were not migrated keep using ``GitRepository.run`` directly and
are inventoried as legacy callers.

``execute_system_safe()`` composes the two existing surfaces for the governed
automation: it issues a ``SYSTEM_SAFE_AUTOMATION`` Tier-2 token for the
operation (only the whitelisted operations are issuable) and executes the
command through the same gate.  Automation can never issue a Tier-3 token.

Tier rules:
  * Tier 1 — read-only, no token, direct execution.
  * Tier 2 — scoped token, or an audited legacy confirmation while callers
    migrate (``approval_path=LEGACY_CONFIRM``); the legacy path is recorded
    in the capability ledger with the ``DEPRECATED_COMPATIBILITY`` marker,
    never silent.
  * Tier 3 — authority-class token only.  A legacy authority flag is refused
    for automation identities (``CAPABILITY_COORDINATOR_SELF_AUTHORIZATION``),
    so a coordinator can never self-authorize history rewriting; without an
    external authority capability the command fails closed.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import classify
from .capability import (
    CapabilityContext,
    CapabilityLedger,
    CapabilityToken,
    IssuerClass,
    LEGACY_COMPATIBILITY_MARKER,
    is_automation_actor,
    repository_id_for,
    verify_capability,
)
from .capability_issue import issue_capability
from .command_normalizer import command_args, normalize_command

__all__ = [
    "GateResult",
    "LEGACY_COMPATIBILITY_MARKER",
    "default_runner",
    "execute_system_safe",
    "execute_with_capability",
]

Runner = Callable[[list[str]], Any]


@dataclass
class GateResult:
    """Outcome of one gated command (allowed or rejected)."""

    allowed: bool
    code: str
    detail: str
    tier: int
    command: str
    normalized: str
    command_digest: str
    operation: str
    capability_id: str = ""
    integrity: str = ""
    consumed: bool = False
    approval_path: str = ""
    command_id: str = ""
    checks: tuple[str, ...] = ()
    execution_result: Any = None
    ledger_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checks"] = list(self.checks)
        data["execution_result"] = repr(self.execution_result)
        return data


def _command_text(command: str | Sequence[str]) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(item) for item in command)
    return str(command)


def default_runner(
    repo_path: str | Path | None, *, tier: int, actor: str,
    capability_id: str = "", approval_path: str = "CAPABILITY_TOKEN",
    timeout: float | None = None,
) -> Runner:
    """Delegate to the gateway's verified-execution path (never a raw spawn).

    The capability (or the audited legacy decision) has already been verified
    by the gate, so the gateway re-checks the tier and records the approval
    path/capability id instead of taking a boolean confirmation.
    """
    from .git_repository import GitRepository

    repo = GitRepository(repo_path or Path.cwd())

    def run(args: list[str]) -> Any:
        return repo._run_verified(
            list(args), tier=tier, actor=actor,
            approval_path=approval_path, capability_id=capability_id,
            timeout=timeout,
        )

    return run


def _gate(
    *, allowed: bool, code: str, detail: str, tier: int, command: str,
    normalized: Any, approval_path: str,
    capability: CapabilityToken | None = None, integrity: str = "",
    consumed: bool = False, command_id: str = "",
    checks: tuple[str, ...] = (), ledger: CapabilityLedger | None = None,
) -> GateResult:
    return GateResult(
        allowed=allowed, code=code, detail=detail, tier=tier,
        command=command, normalized=normalized.text,
        command_digest=normalized.digest, operation=normalized.operation(),
        capability_id=capability.capability_id if capability else "",
        integrity=integrity, consumed=consumed, approval_path=approval_path,
        command_id=command_id, checks=checks,
        ledger_path=str(ledger.path) if ledger else "",
    )


def _dispatch(
    gate: GateResult, *, args: list[str], runner: Runner,
    ledger: CapabilityLedger | None, capability: CapabilityToken | None,
    detail_extra: str = "",
) -> GateResult:
    try:
        gate.execution_result = runner(args)
    except Exception as exc:  # noqa: BLE001 - total gate result
        gate.allowed = False
        gate.code = "EXECUTION_FAILED"
        gate.detail = f"{detail_extra}runner error: {exc}"
        _record_completion(ledger, capability, gate, result="error")
        return gate
    _record_completion(ledger, capability, gate, result="executed")
    return gate


def _record_completion(
    ledger: CapabilityLedger | None, capability: CapabilityToken | None,
    gate: GateResult, *, result: str,
) -> None:
    if ledger is None or capability is None:
        return
    ledger.record_result(
        capability, command_id=gate.command_id,
        result=result, approval_path=gate.approval_path,
    )


def _legacy_decision(
    *, tier: int, actor: str, legacy_confirmed: bool,
    legacy_authority_approved: bool, normalized: Any, command: str,
    repository_id: str, ledger: CapabilityLedger | None,
) -> GateResult:
    command_id = uuid.uuid4().hex
    marker = LEGACY_COMPATIBILITY_MARKER
    if tier == 2 and legacy_confirmed:
        path = "LEGACY_CONFIRM"
        detail = f"tier-2: audited legacy confirmation ({marker})"
    elif tier == 3 and legacy_authority_approved:
        if is_automation_actor(actor):
            return _gate(
                allowed=False, code="CAPABILITY_COORDINATOR_SELF_AUTHORIZATION",
                detail="automation identity may not self-authorize tier-3",
                tier=tier, command=command, normalized=normalized,
                approval_path="", ledger=ledger,
            )
        path = "LEGACY_AUTHORITY"
        detail = f"tier-3: audited legacy authority approval ({marker})"
    else:
        return _gate(
            allowed=False, code="CAPABILITY_MISSING",
            detail=f"tier-{tier} requires a capability token",
            tier=tier, command=command, normalized=normalized,
            approval_path="", ledger=ledger,
        )
    if ledger is not None:
        ledger.record_legacy(
            actor=actor, operation=normalized.operation(),
            command_id=command_id, detail=detail, approval_path=path,
            repository_id=repository_id,
        )
    gate = _gate(
        allowed=True, code="OK", detail=detail, tier=tier, command=command,
        normalized=normalized, approval_path=path,
        command_id=command_id, ledger=ledger,
    )
    return gate


def execute_with_capability(
    command: str | Sequence[str],
    capability: CapabilityToken | None = None,
    *,
    tier: int | None = None,
    actor: str = "unknown",
    repository_id: str = "",
    repo_path: str | Path | None = None,
    worktree_id: str = "",
    branch: str = "",
    target_ref: str = "",
    proposal_generation: int = 0,
    source_revision: str = "",
    target_revision: str = "",
    policy_version: str = "",
    policy_digest: str = "",
    ledger: CapabilityLedger | None = None,
    runner: Runner | None = None,
    now: Any = None,
    require_signature: bool = False,
    legacy_confirmed: bool = False,
    legacy_authority_approved: bool = False,
) -> GateResult:
    """Verify (+consume) a capability, then run the command through the gate.

    Tier 1 needs no token.  Tier 2/3 without a token fall back to the audited
    legacy path; with a token the verification order is exactly
    ``capability.VERIFICATION_ORDER`` and consumption happens last.
    """
    normalized = normalize_command(command)
    resolved_tier = int(tier) if tier is not None else classify(_command_text(command))
    ledger = ledger or CapabilityLedger()
    args = command_args(command)
    command_text = _command_text(command)

    if resolved_tier == 1:
        runner = runner or default_runner(
            repo_path, tier=1, actor=actor, approval_path="TIER1_DIRECT",
        )
        gate = _gate(
            allowed=True, code="OK",
            detail="tier-1: read-only, direct execution (no capability)",
            tier=1, command=command_text, normalized=normalized,
            approval_path="TIER1_DIRECT", ledger=ledger,
        )
        return _dispatch(
            gate, args=args, runner=runner, ledger=None, capability=None,
        )

    if capability is None:
        gate = _legacy_decision(
            tier=resolved_tier, actor=actor, legacy_confirmed=legacy_confirmed,
            legacy_authority_approved=legacy_authority_approved,
            normalized=normalized, command=command_text,
            repository_id=repository_id, ledger=ledger,
        )
        if not gate.allowed:
            return gate
        runner = runner or default_runner(
            repo_path, tier=resolved_tier, actor=actor,
            approval_path=gate.approval_path,
        )
        return _dispatch(
            gate, args=args, runner=runner, ledger=None, capability=None,
        )

    runner = runner or default_runner(
        repo_path, tier=resolved_tier, actor=actor,
        approval_path="CAPABILITY_TOKEN",
        capability_id=capability.capability_id,
    )
    context = CapabilityContext(
        tier=resolved_tier, actor=actor, repository_id=repository_id,
        operation=normalized.operation(), command_digest=normalized.digest,
        worktree_id=worktree_id, branch=branch, target_ref=target_ref,
        source_revision=source_revision, target_revision=target_revision,
        proposal_generation=int(proposal_generation),
        policy_version=policy_version, policy_digest=policy_digest,
    )
    verification = verify_capability(
        capability, context, ledger=ledger, now=now,
        require_signature=require_signature,
    )
    if not verification.allowed:
        if isinstance(capability, CapabilityToken):
            ledger.record_denial(capability, verification.code, verification.detail)
        return GateResult(
            allowed=False, code=verification.code, detail=verification.detail,
            tier=resolved_tier, command=command_text,
            normalized=normalized.text, command_digest=normalized.digest,
            operation=normalized.operation(),
            capability_id=capability.capability_id, integrity=verification.integrity,
            consumed=False, approval_path="CAPABILITY_TOKEN",
            checks=verification.checks, ledger_path=str(ledger.path),
        )
    gate = _gate(
        allowed=True, code="OK", detail="capability verified and consumed",
        tier=resolved_tier, command=command_text, normalized=normalized,
        approval_path="CAPABILITY_TOKEN",
        capability=capability, integrity=verification.integrity,
        consumed=verification.consumed,
        command_id=str(verification.record.get("command_id") or ""),
        checks=verification.checks, ledger=ledger,
    )
    return _dispatch(
        gate, args=args, runner=runner, ledger=ledger, capability=capability,
    )


def _effective_tier(command: str | Sequence[str]) -> int:
    from .git_repository import _extended_tier

    text = _command_text(command)
    extended = _extended_tier(text)
    return extended if extended is not None else classify(text)


def execute_system_safe(
    command: str | Sequence[str],
    *,
    actor: str,
    repo_path: str | Path | None = None,
    repository_id: str = "",
    ledger: CapabilityLedger | None = None,
    timeout: float | None = None,
    **context: Any,
) -> GateResult:
    """Issue a SYSTEM_SAFE_AUTOMATION Tier-2 token and execute through the gate.

    This is the migrated surface for governed automation: the issuer identity
    is derived from (and never equal to) the actor, so the self-issuance rule
    holds structurally, and ``issue_capability`` refuses any operation outside
    ``SYSTEM_SAFE_TIER2_OPERATIONS`` or any Tier-3 command.  Tier-1 commands
    execute without a token; anything classified Tier 3 is refused here and
    must obtain an external governance-authority capability.
    """
    tier = _effective_tier(command)
    ledger = ledger or CapabilityLedger()
    repo_id = repository_id or repository_id_for(repo_path or Path.cwd())
    if tier >= 3:
        return GateResult(
            allowed=False, code="CAPABILITY_ISSUER_NOT_AUTHORIZED",
            detail="automation may not issue tier-3 capability",
            tier=tier, command=_command_text(command),
            normalized=normalize_command(command).text,
            command_digest="", operation="",
            approval_path="", ledger_path=str(ledger.path) if ledger else "",
        )
    if tier == 1:
        return execute_with_capability(
            command, None, tier=1, actor=actor, repository_id=repo_id,
            repo_path=repo_path, ledger=ledger,
            runner=default_runner(
                repo_path, tier=1, actor=actor, approval_path="TIER1_DIRECT",
                timeout=timeout,
            ),
        )
    token = issue_capability(
        issuer_class=IssuerClass.SYSTEM_SAFE_AUTOMATION,
        issuer_id=f"automation/{actor}",
        actor=actor,
        repository_id=repo_id,
        command=command,
        tier=tier,
        ledger=ledger,
    )
    return execute_with_capability(
        command, token, tier=tier, actor=actor, repository_id=repo_id,
        repo_path=repo_path, ledger=ledger, **context,
        runner=default_runner(
            repo_path, tier=tier, actor=actor,
            approval_path="CAPABILITY_TOKEN",
            capability_id=token.capability_id, timeout=timeout,
        ),
    )
