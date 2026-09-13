"""Maintenance health classifier — A258 health classification only.

SCOPE: health monitoring, data-integrity-check, presentation
FORBID: maintenance-code-change, maintenance-permission, parallel-owner, mutation-without-proof
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    HealthSignal,
    HealthState,
)


class MaintenanceHealthClassifier:
    """Maintenance-sovereign: health classification only.

    SCOPE: health monitoring, data-integrity-check, presentation
    FORBID: maintenance-code-change, maintenance-permission, parallel-owner, mutation-without-proof
    """

    def __init__(self, project_root: Path, audit: GovernanceAudit):
        self.project_root = project_root
        self.audit = audit
        self._component_health: dict[str, HealthSignal] = {}
        self._lock = threading.RLock()

    def classify(self, signals: list[HealthSignal]) -> dict[str, Any]:
        """Classify health signals into component states.

        Returns health classification with NO repair decisions.
        """
        with self._lock:
            classification = {
                "overall_state": HealthState.HEALTHY,
                "components": {},
                "degraded_dimensions": [],
                "critical_dimensions": [],
                "classified_at": datetime.now(timezone.utc).isoformat(),
            }

            for signal in signals:
                self._component_health[signal.component_id] = signal
                comp_state = classification["components"].get(signal.component_id, {
                    "state": HealthState.HEALTHY,
                    "dimensions": {}
                })
                comp_state["dimensions"][signal.dimension] = {
                    "state": signal.state.value,
                    "severity": signal.severity,
                    "evidence": signal.evidence,
                }
                # Update component overall state
                if signal.state.value in ["critical", "degraded"]:
                    if signal.severity >= 4:
                        comp_state["state"] = HealthState.CRITICAL
                        classification["critical_dimensions"].append(f"{signal.component_id}.{signal.dimension}")
                    else:
                        comp_state["state"] = HealthState.DEGRADED
                        classification["degraded_dimensions"].append(f"{signal.component_id}.{signal.dimension}")
                classification["components"][signal.component_id] = comp_state

            # Determine overall state
            if classification["critical_dimensions"]:
                classification["overall_state"] = HealthState.CRITICAL
            elif classification["degraded_dimensions"]:
                classification["overall_state"] = HealthState.DEGRADED

            self.audit.record("health_classification", {
                "classification": {k: v.value if isinstance(v, Enum) else v for k, v in classification.items()},
            })

            return classification

    def get_component_health(self, component_id: str) -> Optional[HealthSignal]:
        with self._lock:
            return self._component_health.get(component_id)


__all__ = ["MaintenanceHealthClassifier"]
