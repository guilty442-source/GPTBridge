"""Validation chain signal function — A187/E162.

Information-layer signal for validation chain status.  This module provides
**signal-only** output — it never grants permission, assigns tasks, or
dispatches execution.
"""

from __future__ import annotations

from typing import Any

from core_system.validation_chain_types import (
    VALIDATION_CHAIN_OWNERS,
    VALIDATION_CHAIN_STAGES,
    ChainValidationResult,
)


def validation_chain_signal(
    result: ChainValidationResult,
) -> dict[str, Any]:
    """Produce an information-layer signal for validation chain status (A187/E162).

    Per A187: ``FAILURE:any-gate=>stop+fail-closed+typed-result+audit``.
    """
    return {
        "signal_type": "validation-chain",
        "authority": "signal-only",
        "basis": "A187/E162",
        "ok": result.ok,
        "completed_stages": list(result.completed_stages),
        "failures": list(result.failures),
        "chain_order": list(VALIDATION_CHAIN_STAGES),
        "chain_owners": dict(VALIDATION_CHAIN_OWNERS),
        "separation": "assignment-proof!=permission-proof!=execution-proof",
        "action_required": "fail-closed+typed-result+audit" if not result.ok else "none",
        "gate_skip": False,
        "gate_reorder": False,
        "self_authorize": False,
    }
