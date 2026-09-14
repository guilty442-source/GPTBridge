"""System Runtime Sovereign — Autonomous Supervision (A28/A33/A65)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._base import SovereignBase


# Terminal runtime states that trigger repair routing.
_DEGRADED_STATES = frozenset({"degraded", "failed", "dead", "unknown"})
_SERVING_STATES = frozenset({"serving", "ready", "active", "converged"})

# Runtime readiness state file (information-layer, A67).
_READINESS_STATE_RELATIVE = (
    "main-system",
    "runtime",
    "state",
    "runtime-readiness.json",
)


class SystemRuntimeAutonomyMixin:
    """Full-automation auto-loop: monitors coverage, readiness, child health, process survival, convergence."""

    _auto_loop_task: asyncio.Task[Any] | None
    _auto_loop_interval: float
    _auto_enabled: bool
    _auto_metrics: dict[str, Any]
    app: Any
    _started: bool
    _runtime_state: str

    def _start_autonomy_loop(self) -> None:
        if self._auto_loop_task is None or self._auto_loop_task.done():
            self._auto_enabled = True
            try:
                self._auto_loop_task = asyncio.create_task(
                    self._auto_loop(),
                    name="system-runtime-autonomy",
                )
            except RuntimeError:
                self._auto_loop_task = None

    async def _stop_autonomy_loop(self) -> None:
        task = self._auto_loop_task
        self._auto_loop_task = None
        self._auto_enabled = False
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _auto_loop(self) -> None:
        while self._auto_enabled:
            try:
                await self._auto_tick()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
                pass
            try:
                await asyncio.sleep(self._auto_loop_interval)
            except asyncio.CancelledError:
                raise

    async def _auto_tick(self) -> None:
        await self._check_coverage()
        await self._poll_runtime_state()
        await self._supervise_children()
        self._persist_live_state()

    async def _check_coverage(self) -> None:
        """Check sub-sovereign coverage and route gaps to decision-sovereign."""
        from governance.registries import children_of

        self._auto_metrics["coverage_checks"] += 1
        expected = set(children_of("system-runtime-sovereign"))
        materialized = set(self._sub_sovereigns.keys())
        missing = expected - materialized
        if missing:
            self._auto_metrics["gap_repairs_routed"] += len(missing)
            # Route to decision-sovereign repair chain
            decision_sovereign = getattr(self.app, "decision_sovereign", None)
            if decision_sovereign:
                for child_id in missing:
                    await decision_sovereign.handle(
                        SovereignRequest(
                            intent="repair.decide-and-route",
                            subject=f"missing-child-{child_id}",
                            requester="system-runtime-autonomy",
                            payload={"classified_signal": {"type": "missing-child", "child_id": child_id}},
                        ),
                    )

    async def _poll_runtime_state(self) -> None:
        """Poll runtime readiness state from information layer (A67)."""
        self._auto_metrics["runtime_state_polls"] += 1
        project_root = getattr(self.app, "project_root", None)
        if not project_root:
            return
        readiness_path = Path(project_root) / _READINESS_STATE_RELATIVE[0] / _READINESS_STATE_RELATIVE[1] / _READINESS_STATE_RELATIVE[2] / _READINESS_STATE_RELATIVE[3]
        try:
            import json
            state = json.loads(readiness_path.read_text(encoding="utf-8"))
            new_state = str(state.get("runtime_state", "")).lower()
            if new_state and new_state != self._runtime_state:
                old_state = self._runtime_state
                self._runtime_state = new_state
                if new_state in _DEGRADED_STATES and old_state in _SERVING_STATES:
                    self._auto_metrics["degradation_detected"] += 1
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

    async def _supervise_children(self) -> None:
        """Detect stopped children and adjudicate bounded restarts."""
        from governance.registries import parent_of, resolve_sovereign

        now = time.monotonic()
        for child_id, child in self._all_children().items():
            if bool(getattr(child, "_started", False)):
                watch = self._child_supervision.get(child_id)
                if watch is not None and watch.get("state") != "started":
                    watch["state"] = "started"
                    watch["recovered_at"] = self._iso_now()
                    watch.pop("quarantined", None)
                continue

            watch = self._child_supervision.setdefault(
                child_id, {"state": "started", "restart_attempts": 0}
            )
            if watch.get("state") == "started":
                parent_id = parent_of(child_id)
                parent = (
                    self
                    if parent_id == self.sovereign_id
                    else resolve_sovereign(self.app, parent_id)
                )
                if parent is not None:
                    try:
                        parent.record_child_failure(child_id)
                    except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
                        pass
                watch["state"] = "stopped"
                watch["stopped_at"] = self._iso_now()

            if watch.get("quarantined"):
                continue
            parent_id = parent_of(child_id)
            parent = (
                self
                if parent_id == self.sovereign_id
                else resolve_sovereign(self.app, parent_id)
            )
            if parent is None:
                continue
            if parent.child_failure_count(child_id) > 3:
                watch["quarantined"] = True
                watch["quarantined_at"] = self._iso_now()
                self._auto_metrics["child_quarantines"] += 1
                continue
            last_attempt = float(watch.get("last_attempt") or 0.0)
            if now - last_attempt < 60.0:
                continue
            executor = getattr(self.app, "sovereign_stack_executor", None)
            if executor is None:
                continue
            watch["last_attempt"] = now
            watch["restart_attempts"] = int(watch.get("restart_attempts") or 0) + 1
            self._auto_metrics["child_retries_triggered"] += 1
            try:
                watch["last_result"] = await executor.restart_child(self, child_id)
            except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError) as error:
                watch["last_result"] = {
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }

    def _persist_live_state(self) -> None:
        """Keep live state persisted."""
        try:
            state = self._load_state()
            state.update({
                "runtime_state": self._runtime_state,
                "autonomy_metrics": dict(self._auto_metrics),
                "child_supervision": dict(self._child_supervision),
            })
            self._save_state(state)
        except OSError:
            pass