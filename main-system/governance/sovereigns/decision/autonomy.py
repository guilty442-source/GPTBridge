"""Decision Sovereign — Autonomous Supervision (A322/A334).

Continuous supervision of child sub-sovereigns: detects stopped children,
adjudicates bounded restarts per codex parent's failure counter, reconciles
certified-update lifecycle, and persists live state.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .._base import SovereignBase
from core_system.sovereign_utils import _iso_now


# Supervision cadence
_AUTONOMY_INTERVAL_SECONDS = 15.0
_CHILD_RESTART_BUDGET = 3
_CHILD_RESTART_COOLDOWN_SECONDS = 60.0


class DecisionAutonomyMixin:
    """Autonomous supervision loop (decision-layer only)."""

    _autonomy_task: asyncio.Task[Any] | None
    _autonomy_stop: asyncio.Event
    _child_supervision: dict[str, dict[str, Any]]
    app: Any
    _started: bool
    _certified_updates: dict[str, Any]

    def _start_autonomy_loop(self) -> None:
        if self._autonomy_task is None or self._autonomy_task.done():
            self._autonomy_stop.clear()
            try:
                self._autonomy_task = asyncio.create_task(
                    self._autonomy_loop(),
                    name="decision-sovereign-autonomy",
                )
            except RuntimeError:
                self._autonomy_task = None

    async def _stop_autonomy_loop(self) -> None:
        task = self._autonomy_task
        self._autonomy_task = None
        self._autonomy_stop.set()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _autonomy_loop(self) -> None:
        while not self._autonomy_stop.is_set():
            try:
                await self._autonomy_tick()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
                pass
            try:
                await asyncio.wait_for(
                    self._autonomy_stop.wait(),
                    timeout=_AUTONOMY_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _autonomy_tick(self) -> None:
        await self._supervise_children()
        self._reconcile_certified_updates()
        self._persist_live_state()

    async def _supervise_children(self) -> None:
        """Detect stopped children and adjudicate bounded restarts (A322)."""
        from governance.registries import parent_of, resolve_sovereign

        now = time.monotonic()
        for child_id, child in self._all_children().items():
            if bool(getattr(child, "_started", False)):
                watch = self._child_supervision.get(child_id)
                if watch is not None and watch.get("state") != "started":
                    watch["state"] = "started"
                    watch["recovered_at"] = _iso_now()
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
                watch["stopped_at"] = _iso_now()

            await self._attempt_child_restart(child_id, watch, now)

    async def _attempt_child_restart(
        self, child_id: str, watch: dict, now: float
    ) -> None:
        from governance.registries import parent_of, resolve_sovereign

        if watch.get("quarantined"):
            return
        parent_id = parent_of(child_id)
        parent = (
            self
            if parent_id == self.sovereign_id
            else resolve_sovereign(self.app, parent_id)
        )
        if parent is None:
            return
        if parent.child_failure_count(child_id) > _CHILD_RESTART_BUDGET:
            watch["quarantined"] = True
            watch["quarantined_at"] = _iso_now()
            return
        last_attempt = float(watch.get("last_attempt") or 0.0)
        if now - last_attempt < _CHILD_RESTART_COOLDOWN_SECONDS:
            return
        executor = getattr(self.app, "sovereign_stack_executor", None)
        if executor is None:
            return
        watch["last_attempt"] = now
        watch["restart_attempts"] = int(watch.get("restart_attempts") or 0) + 1
        try:
            watch["last_result"] = await executor.restart_child(self, child_id)
        except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError) as error:
            watch["last_result"] = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
            }

    def _pending_repair_requests(self) -> int:
        from pathlib import Path
        import json
        path = (
            self.workspace_root
            / "main-system"
            / "runtime"
            / "state"
            / "repair-requests.json"
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return 0
        requests = (
            payload.get("requests") if isinstance(payload, dict) else payload
        )
        if not isinstance(requests, list):
            return 0
        return sum(
            1
            for item in requests
            if isinstance(item, dict)
            and str(item.get("status") or "") == "pending"
        )

    def _persist_live_state(self) -> None:
        """Keep decision-sovereign.json live between start and stop."""
        try:
            state = self._load_state()
            state.update(
                {
                    "sovereign": "decision-sovereign",
                    "started": self._started,
                    "heartbeat_at": _iso_now(),
                    "autonomy": {
                        "enabled": True,
                        "interval_seconds": _AUTONOMY_INTERVAL_SECONDS,
                        "supervised_children": {
                            child_id: dict(watch)
                            for child_id, watch in
                            self._child_supervision.items()
                        },
                        "pending_repair_requests":
                            self._pending_repair_requests(),
                        "certified_updates_active": sum(
                            1
                            for record in self._certified_updates.values()
                            if record.get("terminal_status")
                            not in _TERMINAL_STATUSES
                        ),
                    },
                }
            )
            self._save_state(state)
        except OSError:
            pass