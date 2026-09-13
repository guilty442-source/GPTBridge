"""Xingcheng Identity Layer — role and identity management for the local model.

Per the Governance Codex (A18), Xingcheng is a local-native model with
top-orchestrator rank.  Its identity layer manages the model's role declaration
and identity binding — it does NOT manage system permission IDs (those are
owned by the permission-sovereign per A23).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

XINGCHENG_ID: Final[str] = "xingcheng"
XINGCHENG_ROLE: Final[str] = "top-orchestrator"
XINGCHENG_RANK: Final[str] = "auxiliary-system"
XINGCHENG_BASIS: Final[str] = "codex"


@dataclass(frozen=True)
class XingchengIdentity:
    """Immutable identity declaration for the Xingcheng model."""
    id: str = XINGCHENG_ID
    role: str = XINGCHENG_ROLE
    rank: str = XINGCHENG_RANK
    basis: str = XINGCHENG_BASIS
    has_execution_power: bool = False  # A21: no direct execution


def identity_snapshot() -> XingchengIdentity:
    """Return the Xingcheng identity snapshot."""
    return XingchengIdentity()


__all__ = [
    "XINGCHENG_BASIS",
    "XINGCHENG_ID",
    "XINGCHENG_RANK",
    "XINGCHENG_ROLE",
    "XingchengIdentity",
    "identity_snapshot",
]
