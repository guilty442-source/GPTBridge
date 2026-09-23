"""Fault diagnostics — governed read-only evidence collection for 星澄.

Provides Xingcheng with grounded fault-investigation capability:

  - Matches a symptom against the authoritative ``fault_code_directory``
    and ``maintenance_manual_directory`` in the sealed governance codex
    (opened read-only/immutable; xingcheng is a registered read audience).
  - Reads main-system runtime state files (boot core, readiness, IPC
    connection state, repair coordination) as read-only evidence.
  - Reads the A195 transactional outbox tail for recent committed state
    transitions.

Boundary: diagnosis is read-only and advisory.  Repair execution stays
with main-system central repair through the governed channel; Xingcheng
reports findings and governed remediation steps only.
"""

from __future__ import annotations
from pathlib import Path

from .fault_diagnostics_aux import FaultDiagnosticsAuxMixin
from .fault_diagnostics_evidence import FaultDiagnosticsEvidenceMixin
from .fault_diagnostics_localize import FaultDiagnosticsLocalizeMixin
from .fault_diagnostics_data import (
    _FAULT_KEYWORDS,
    _STATE_RELATIVE,
)


class FaultDiagnostics(
    FaultDiagnosticsAuxMixin,
    FaultDiagnosticsEvidenceMixin,
    FaultDiagnosticsLocalizeMixin,
):
    """Read-only fault-diagnosis evidence collector for Xingcheng."""

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self.state_dir = self.project_root.joinpath(*_STATE_RELATIVE)

    def _codex_available(self) -> bool:
        """True when the governed PostgreSQL codex authority answers."""
        try:
            from governance_rule.execution.codex_postgresql import (
                authority_state,
            )

            authority_state()
            return True
        except Exception:
            return False

    @staticmethod
    def looks_like_fault(text: str) -> bool:
        """Heuristic gate: does this question ask about a system fault?"""
        candidate = str(text or "")
        if not candidate.strip():
            return False
        lowered = candidate.casefold()
        return any(keyword in lowered or keyword in candidate
                   for keyword in _FAULT_KEYWORDS)


__all__ = ["FaultDiagnostics"]
