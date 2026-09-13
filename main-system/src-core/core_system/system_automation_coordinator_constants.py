"""System automation coordinator — constants."""

from __future__ import annotations

# Coordination loop interval (seconds).
_COORDINATOR_INTERVAL_SECONDS = 15.0

# Sovereigns managed by the coordinator (in startup order).
_SOVEREIGN_ATTRS = (
    "decision_sovereign",
    "permission_sovereign",
    "system_runtime_sovereign",
    "synchronization_sovereign",
    "xingcheng_sovereign",
)
