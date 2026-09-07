"""Local Model Hub — central model routing and management.

Per the local-model manifest, the hub provides:
  - model loading and selection
  - inference routing
  - runtime lifecycle management
  - context-aware multitask platform coordination

This module is LOCAL CODE; actual model inference is delegated to the
governed executor (Ollama loopback runtime).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

HUB_ID: Final[str] = "local-model-hub"
PLATFORM_ID: Final[str] = "local-model-platform"


@dataclass(frozen=True)
class ModelRoute:
    """A model routing declaration (read-only, decision-level)."""
    task_type: str
    model_id: str
    role: str
    priority: int = 0


@dataclass
class HubStatus:
    """Local model hub status snapshot."""
    platform_id: str = PLATFORM_ID
    started: bool = False
    resident_model: str = ""
    active_routes: list[ModelRoute] = field(default_factory=list)
    basis: str = "codex"


__all__ = [
    "HUB_ID",
    "HubStatus",
    "ModelRoute",
    "PLATFORM_ID",
]
