"""Model lifecycle management — loading, selection, and runtime coordination.

Per the local-model manifest, the hub owns model-loading, model-selection,
inference, and runtime-lifecycle.  This module provides the decision-level
lifecycle surface; actual model loading is delegated to the Ollama loopback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

RESIDENT_MODEL: Final[str] = "qwen3.5:9b-q4_K_M"
MAX_CONCURRENT_TRANSFORMERS: Final[int] = 4
KEEP_ALIVE: Final[int] = -1  # permanent


@dataclass
class ModelLifecycleState:
    """Model lifecycle state (decision-level)."""
    resident_model: str = RESIDENT_MODEL
    loaded_models: list[str] = field(default_factory=list)
    max_concurrent: int = MAX_CONCURRENT_TRANSFORMERS
    keep_alive: int = KEEP_ALIVE
    started: bool = False
    basis: str = "codex"


def initial_state() -> ModelLifecycleState:
    """Return the initial model lifecycle state."""
    return ModelLifecycleState(
        loaded_models=[RESIDENT_MODEL],
        started=True,
    )


__all__ = [
    "KEEP_ALIVE",
    "MAX_CONCURRENT_TRANSFORMERS",
    "ModelLifecycleState",
    "RESIDENT_MODEL",
    "initial_state",
]
