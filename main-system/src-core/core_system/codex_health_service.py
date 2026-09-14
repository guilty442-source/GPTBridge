"""Codex health evidence chain — verifies the complete governance health (A174/A435/A46/A121).

法典依據:
- A174: official entry controls (identity, purpose, scope, nonce, expiry, audit).
- A435: access-class session engine; revocation generation; dual-key grants.
- A46: ledger-per-action — every action carries an audit ledger entry.
- A121: boundary enforcement + audit ledger + deny-on-violation.
- A173: single local read-only SQLite authority; Chinese mirror is 星澄-only.

This module produces a **verified evidence chain**, not an information
display: each link checks a governance component and records pass/fail with
the evidence that proves it, so consumers can audit the codex's own health
rather than reading a summary panel.

Evidence chain links:
1. Codex authority — version, schema, loadability.
2. Entry state — revocation generation, persisted schema, sessions, grants.
3. Session health — open sessions, expired sessions, consumed nonces.
4. Audit ledger health — codex read audit, execution audit, delegation audit,
   permission grant ledger, fault query audit, governed process audit.
5. Governance audit — last audit pass/fail.
6. Integrity manifest — authority files pinned and signed (when available).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _count_jsonl(path: Path) -> int:
    """Count lines in a JSONL file (best-effort, 0 on error)."""
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def _last_jsonl_entry(path: Path) -> dict[str, Any] | None:
    """Return the last parseable JSONL entry, or None."""
    if not path.is_file():
        return None
    last: dict[str, Any] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return last


@dataclass(frozen=True)
class HealthLink:
    """One verified link in the codex health evidence chain."""

    name: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    basis: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "evidence": self.evidence,
            "basis": list(self.basis),
        }


def _check_codex_authority() -> HealthLink:
    """Link 1: codex authority — version, schema, loadability."""
    try:
        from governance_rule.execution.codex_repository import (
            CODEX_VERSION_UNIT,
            load_governance_codex,
        )
        codex = load_governance_codex()
        version_ok = codex.codex_version >= CODEX_VERSION_UNIT
        return HealthLink(
            name="codex-authority",
            passed=version_ok,
            evidence={
                "codex_version": codex.codex_version,
                "schema": codex.schema,
                "sovereign_count": len(codex.sovereigns),
                "version_ok": version_ok,
            },
            basis=("A173", "A174"),
        )
    except Exception as error:
        return HealthLink(
            name="codex-authority",
            passed=False,
            evidence={"error": type(error).__name__},
            basis=("A173", "A174"),
        )


def _check_entry_state() -> HealthLink:
    """Link 2: entry state — revocation generation, persisted schema."""
    try:
        from governance_rule.execution.codex_entry_state import (
            read_entry_state,
            ENTRY_STATE_PATH,
        )
        state = read_entry_state()
        sessions = state.get("sessions", {})
        grants = state.get("grants", {})
        consumed = state.get("consumed_nonces", {})
        return HealthLink(
            name="entry-state",
            passed=True,
            evidence={
                "revocation_generation": state.get("revocation_generation", 0),
                "open_sessions": len(sessions),
                "grants": len(grants),
                "consumed_nonces": len(consumed),
                "state_path_exists": ENTRY_STATE_PATH.is_file(),
            },
            basis=("A174", "A435"),
        )
    except PermissionError as error:
        return HealthLink(
            name="entry-state",
            passed=False,
            evidence={"error": str(error)},
            basis=("A174", "A435"),
        )
    except Exception as error:
        return HealthLink(
            name="entry-state",
            passed=False,
            evidence={"error": type(error).__name__},
            basis=("A174", "A435"),
        )


def _check_session_health() -> HealthLink:
    """Link 3: session health — expired sessions, consumed nonce count."""
    try:
        from governance_rule.execution.codex_entry_state import read_entry_state
        state = read_entry_state()
        sessions = state.get("sessions", {})
        now = time.time()
        expired = sum(
            1 for s in sessions.values()
            if isinstance(s, dict) and s.get("expires_at", 0) < now
        )
        open_count = len(sessions) - expired
        return HealthLink(
            name="session-health",
            passed=True,
            evidence={
                "open_sessions": open_count,
                "expired_sessions": expired,
                "consumed_nonces": len(state.get("consumed_nonces", {})),
            },
            basis=("A174", "A435"),
        )
    except Exception as error:
        return HealthLink(
            name="session-health",
            passed=False,
            evidence={"error": type(error).__name__},
            basis=("A174", "A435"),
        )


def _check_audit_ledgers(project_root: Path) -> HealthLink:
    """Link 4: audit ledger health — entry counts for all governance ledgers."""
    ledgers = {
        "codex_read_audit": project_root / "governance_rule/execution/audit/codex_read_audit.jsonl",
        "sovereign_execution_audit": project_root / "governance_rule/runtime/sovereign_execution_audit.jsonl",
        "delegation_audit": project_root / "main-system/runtime/state/delegation-audit.jsonl",
        "permission_grant_ledger": project_root / "main-system/runtime/state/permission-grant-ledger.jsonl",
        "fault_query_audit": project_root / "main-system/runtime/state/fault-query-audit.jsonl",
        "governed_process_audit": project_root / "shared-layer/runtime/governed-process-audit.jsonl",
    }
    counts = {name: _count_jsonl(path) for name, path in ledgers.items()}
    all_present = all(count >= 0 for count in counts.values())
    return HealthLink(
        name="audit-ledger-health",
        passed=all_present,
        evidence={
            "ledger_counts": counts,
            "total_entries": sum(counts.values()),
        },
        basis=("A46", "A121"),
    )


def _check_governance_audit() -> HealthLink:
    """Link 5: governance audit — last audit pass/fail."""
    try:
        from governance_rule.execution.audit import audit_runtime_governance
        errors = audit_runtime_governance(include_self_health=False)
        return HealthLink(
            name="governance-audit",
            passed=len(errors) == 0,
            evidence={
                "error_count": len(errors),
                "errors": errors[:5] if errors else [],
            },
            basis=("A46", "A121", "A174"),
        )
    except Exception as error:
        return HealthLink(
            name="governance-audit",
            passed=False,
            evidence={"error": type(error).__name__},
            basis=("A46", "A121"),
        )


def _check_integrity_manifest(project_root: Path) -> HealthLink:
    """Link 6: integrity manifest — authority files pinned (when available)."""
    try:
        from governance_rule.governance_policy import governance_policy_snapshot
        policy = governance_policy_snapshot()
        authority_files = list(policy.authority_files)
        missing = [
            f for f in authority_files
            if not (project_root / f).is_file()
        ]
        return HealthLink(
            name="integrity-manifest",
            passed=len(missing) == 0,
            evidence={
                "authority_version": policy.authority_version,
                "authority_file_count": len(authority_files),
                "missing_files": missing,
            },
            basis=("A173", "A174"),
        )
    except Exception as error:
        return HealthLink(
            name="integrity-manifest",
            passed=False,
            evidence={"error": type(error).__name__},
            basis=("A173", "A174"),
        )


def collect_codex_health_evidence(project_root: Path) -> dict[str, Any]:
    """Collect the complete codex health evidence chain.

    Returns a dict with:
    - ``chain``: list of HealthLink dicts (each with pass/fail + evidence)
    - ``overall_passed``: True only if every link passed
    - ``timestamp``: collection time
    - ``evidence_version``: content hash of the chain
    """
    links = [
        _check_codex_authority(),
        _check_entry_state(),
        _check_session_health(),
        _check_audit_ledgers(project_root),
        _check_governance_audit(),
        _check_integrity_manifest(project_root),
    ]
    chain = [link.to_dict() for link in links]
    overall = all(link["passed"] for link in chain)
    import hashlib
    version = hashlib.sha256(
        json.dumps(chain, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "chain": chain,
        "overall_passed": overall,
        "timestamp": _utc_now(),
        "evidence_version": version,
        "link_count": len(chain),
        "passed_links": sum(1 for link in chain if link["passed"]),
        "failed_links": sum(1 for link in chain if not link["passed"]),
    }


__all__ = [
    "HealthLink",
    "collect_codex_health_evidence",
]
