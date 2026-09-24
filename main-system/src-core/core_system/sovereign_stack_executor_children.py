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

        A592/A604: the sub-sovereign layer is eliminated, so the start set
        is derived solely from ``children_of`` active hierarchy rows —
        retired identities are never dispatched (FORBID:sub-sovereign-routing).
        """
        import time as _time

        from core_system.sovereign_utils import _iso_now

        dependency_state = sovereign._dependency_state()

        _child_timings: dict[str, int] = {}

        async def _timed_child(
            tag: str, child_id: str
        ) -> dict[str, Any]:
            _t0 = _time.monotonic()
            try:
                return await self._start_child(sovereign, tag, child_id)
            finally:
                _child_timings[child_id] = int(
                    (_time.monotonic() - _t0) * 1000
                )

        targets: list[str] = []
        seen: set[str] = set()
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
                if cid in seen:
                    continue
                seen.add(cid)
                child = getattr(parent, "_sub_sovereigns", {}).get(cid)
                if child is None or not getattr(child, "_started", False):
                    targets.append(cid)

        results: list[dict[str, Any]] = []
        if targets:
            results = list(
                await asyncio.gather(
                    *(_timed_child(cid, cid) for cid in targets)
                )
            )
        if _child_timings:
            self.app._log(
                {"type": "child_start_timings", **_child_timings}
            )

        sub_sovereign_roles = [
            result.get("role", "") for result in results if result
        ]

        # A604: the retired learning/programming identities are reported as
        # retired lineage, never as routing targets.
        def _peer_state(child_id: str) -> dict[str, Any]:
            child = None
            for parent_id in ("automation-sovereign", "星澄"):
                parent = self._parent_object(sovereign, parent_id)
                if parent is not None:
                    found = getattr(parent, "_sub_sovereigns", {}).get(child_id)
                    if found is not None:
                        child = found
                        break
            if child is not None:
                return {"enabled": bool(getattr(child, "_started", False))}
            status = "retired" if self._child_retired(child_id) else "unavailable"
            return {"enabled": False, "registry_status": status}

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
            "retired_children": sorted(set(self._retired_children)),
            "peer_systems": {
                "learning": _peer_state("learning-evidence-sync-sub-sovereign"),
                "programming": _peer_state("release-update-sync-sub-sovereign"),
            },
            "health_owner": "none-sub-sovereign-layer-eliminated-A592-A604",
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
