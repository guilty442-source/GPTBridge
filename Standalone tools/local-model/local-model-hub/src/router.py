"""Model router — context-aware multitask model selection.

Implements the routing logic declared in the local-model manifest:
  traditional-chinese-understand-intent-classify-intensity-route-execute-verify-result

All routing decisions reference the codex as the basis; actual model inference
is delegated to the Ollama loopback governed executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from . import ModelRoute

DEFAULT_ROUTES: Final[tuple[ModelRoute, ...]] = (
    ModelRoute(task_type="understanding", model_id="qwen3.8:27b-q4_K_M", role="command-understanding", priority=10),
    ModelRoute(task_type="execution", model_id="qwen3.6:35b-a3b-coding", role="main-engineer", priority=9),
    ModelRoute(task_type="inspection", model_id="qwen3.8:27b-q4_K_M", role="inspector", priority=8),
    ModelRoute(task_type="integration", model_id="qwen3.8:27b-q4_K_M", role="integrator", priority=8),
    ModelRoute(task_type="data", model_id="ibm/granite4.2:30b-q4_K_M", role="data-worker", priority=7),
    ModelRoute(task_type="visual", model_id="openbmb/minicpm-v4.6:q8_0", role="visual-specialist", priority=6),
    ModelRoute(task_type="reasoning", model_id="deepseek-r1:14b", role="reasoning", priority=7),
    ModelRoute(task_type="training", model_id="gpt-oss:20b", role="training-owner", priority=5),
    ModelRoute(task_type="general", model_id="qwen3.5:9b-q4_K_M", role="resident-generalist", priority=1),
)


def route_for(task_type: str) -> ModelRoute | None:
    """Return the model route for a given task type."""
    task_lower = task_type.lower()
    for route in DEFAULT_ROUTES:
        if route.task_type == task_lower:
            return route
    # Fallback to resident generalist
    for route in DEFAULT_ROUTES:
        if route.task_type == "general":
            return route
    return None


def all_routes() -> tuple[ModelRoute, ...]:
    """Return all declared model routes."""
    return DEFAULT_ROUTES


__all__ = [
    "DEFAULT_ROUTES",
    "all_routes",
    "route_for",
]
