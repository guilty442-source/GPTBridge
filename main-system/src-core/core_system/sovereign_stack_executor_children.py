"""Sovereign Stack Executor — children startup mixin.

Provides the _start_children method for the SovereignStackExecutor.
"""

from __future__ import annotations

import asyncio
from typing import Any

from governance.registries import children_of


class SovereignStackChildrenMixin:
    """Children startup methods for SovereignStackExecutor."""

    async def _start_children(self, sovereign: Any) -> dict[str, Any]:
        """Start the remaining registry children under parent authorization.

        Single-fault isolation: each sub-sovereign is started independently.
        A failure in one does not prevent the rest from starting, and all
        failures are recorded in the report's ``startup_failures`` list.
        """
        from core_system.sovereign_utils import _iso_now

        dependency_state = sovereign._dependency_state()

        runtime, resource, data, integration, third_party = (
            await asyncio.gather(
                self._start_child(
                    sovereign, "runtime", "runtime-state-sync-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "resource", "resource-dependency-sync-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "data", "data-governance-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "integration", "channel-contract-sync-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "third_party", "dependency-sync-sub-sovereign"
                ),
            )
        )

        remaining: list[Any] = []
        for parent_id in (
            "system-runtime-sovereign",
            "permission-sovereign",
            "decision-sovereign",
            "automation-sovereign",
            # A485: 星澄 manages the learning sub-sovereign.
            "星澄",
        ):
            parent = self._parent_object(sovereign, parent_id)
            if parent is None:
                continue
            for cid in children_of(parent_id):
                child = getattr(parent, "_sub_sovereigns", {}).get(cid)
                if child is not None and not getattr(child, "_started", False):
                    remaining.append(self._start_child(sovereign, cid, cid))
        if remaining:
            await asyncio.gather(*remaining)

        sub_sovereign_roles = [
            result.get("role", "")
            for result in (
                runtime,
                resource,
                data,
                integration,
                third_party,
            )
            if result
        ]

        automation = getattr(self.app, "automation_sovereign", None)
        sync_children = (
            getattr(automation, "_sub_sovereigns", {})
            if automation is not None
            else {}
        )
        # A485: the learning sub-sovereign is managed exclusively by 星澄;
        # automation/synchronization retains no learning management authority.
        xingcheng = getattr(self.app, "xingcheng_sovereign", None)
        learning = (
            getattr(xingcheng, "_sub_sovereigns", {}).get(
                "learning-evidence-sync-sub-sovereign"
            )
            if xingcheng is not None
            else None
        )
        programming = sync_children.get("release-update-sync-sub-sovereign")

        report = {
            "ok": len(self._startup_failures) == 0,
            "sovereign": "decision-sovereign",
            "dependency_state": dependency_state,
            "started_at": _iso_now(),
            "execution_delegation": "governed-executor-only",
            "sub_sovereigns": sorted(
                cid
                for cid in children_of("decision-sovereign")
                if cid in getattr(sovereign, "_sub_sovereigns", {})
            ),
            "dispatched_sub_sovereigns": sub_sovereign_roles,
            "startup_failures": list(self._startup_failures),
            "peer_systems": {
                "learning": getattr(learning, "_started", False),
                "programming": getattr(programming, "_started", False),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "sources": [
                {"kind": "env", "name": "GPTBRIDGE_STARTUP_STATE"},
                {
                    "kind": "report",
                    "path": str(sovereign.launcher_report_path),
                },
            ],
        }
        sovereign._save_state(report)
        return report
