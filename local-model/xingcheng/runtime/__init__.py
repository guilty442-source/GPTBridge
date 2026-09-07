"""Xingcheng Runtime Layer — model state and checkpoint management.

Per the Governance Codex (A18/A19), Xingcheng references the codex for all
thinking and management.  This runtime layer manages model state persistence,
checkpoints, and the interface to the local-model governed executor.

This module is LOCAL CODE; actual model inference is delegated to the
governed executor (local-model runtime process).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

XINGCHENG_RUNTIME_DIR: Final[str] = "local-model/xingcheng/runtime"
XINGCHENG_STATE_DIR: Final[str] = "local-model/xingcheng/runtime/state"
XINGCHENG_MODELS_DIR: Final[str] = "local-model/xingcheng/runtime/state/models"


@dataclass(frozen=True)
class ModelCheckpoint:
    """A model checkpoint reference (read-only, decision-level)."""
    name: str
    path: str
    size_bytes: int = 0


@dataclass
class XingchengRuntimeState:
    """Xingcheng runtime state snapshot (decision-level, no execution)."""
    started: bool = False
    model_loaded: bool = False
    checkpoints: list[ModelCheckpoint] = field(default_factory=list)
    basis: str = "codex"


def scan_checkpoints(project_root: Path) -> list[ModelCheckpoint]:
    """Scan the Xingcheng models directory for available checkpoints."""
    models_dir = project_root / XINGCHENG_MODELS_DIR
    if not models_dir.is_dir():
        return []
    checkpoints: list[ModelCheckpoint] = []
    for entry in sorted(models_dir.iterdir()):
        if entry.is_file():
            checkpoints.append(
                ModelCheckpoint(
                    name=entry.name,
                    path=str(entry.relative_to(project_root)),
                    size_bytes=entry.stat().st_size,
                )
            )
    return checkpoints


def runtime_state(project_root: Path) -> XingchengRuntimeState:
    """Return a Xingcheng runtime state snapshot."""
    return XingchengRuntimeState(
        checkpoints=scan_checkpoints(project_root),
    )


__all__ = [
    "ModelCheckpoint",
    "XINGCHENG_MODELS_DIR",
    "XINGCHENG_RUNTIME_DIR",
    "XINGCHENG_STATE_DIR",
    "XingchengRuntimeState",
    "runtime_state",
    "scan_checkpoints",
]