"""Xingcheng Cognition Layer — local-native model reasoning core.

Per the Governance Codex (A18/A19/A20/A21, E5/E13/E14), Xingcheng is a
local-native model with top-orchestrator rank.  Its cognition layer provides
observation, analysis, reasoning, advice, coordination, and explanation — all
referencing the codex as the sole decision basis.

This module is LOCAL CODE and declares the cognition surface; actual model
inference is delegated to the governed executor (local-model runtime).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Tuple

XINGCHENG_POWERS: Final[Tuple[str, ...]] = (
    "observe",
    "analyze",
    "reason",
    "advise",
    "coordinate",
    "explain",
)

XINGCHENG_PROHIBITIONS: Final[Tuple[str, ...]] = (
    "direct-execution",
    "grant-authorization",
    "override-decision",
    "override-state",
    "self-reason-override-codex",
    "hold-system-exec",
    "proxy-integration-structural-interface",
)


@dataclass(frozen=True)
class CognitionRequest:
    """A cognition request to the Xingcheng model (decision-layer only)."""
    action: str  # one of XINGCHENG_POWERS
    context: str = ""
    basis: str = "codex"


@dataclass(frozen=True)
class CognitionResult:
    """A cognition result from the Xingcheng model (advisory only, no execution)."""
    action: str
    output: str
    basis: str = "codex"
    executable: bool = False  # always False — Xingcheng has no execution power


def validate_power(action: str) -> bool:
    """Check if an action is within Xingcheng's declared powers (A20)."""
    return action in XINGCHENG_POWERS


__all__ = [
    "CognitionRequest",
    "CognitionResult",
    "XINGCHENG_POWERS",
    "XINGCHENG_PROHIBITIONS",
    "validate_power",
]
