"""Git Control Plane — unified read-only aggregation of Git runtime state.

§162-217.  The control plane observes, judges and proposes — it never
executes Git writes itself:

    collect_*()          -> dimensional facts (per source of truth)
    evaluate_global_state() -> deterministic HEALTHY..ERROR
    snapshot()           -> Unified Git Snapshot with a generation counter
    build_action_proposals() -> ActionProposal (expires, generation-bound)
    request_action()     -> validate + record decision; NEVER executes.
                            Tier 3 always returns PROPOSAL_PENDING_AUTHORITY.

Sources of truth are strictly separated (§166): Git refs/HEAD for
revisions, the pool registry for workers, the merge queue for
integration state, the audit chain for governed evidence, the recovery
store for recovery evidence.  An event bus (§167-168) invalidates the
fast-state cache but never replaces verified Git state (§169).

Compatibility surface (A185 split): value types live in
``git_control_plane_types``; the method groups live in the mixin modules
``git_control_plane_collectors`` (truth collectors + digests),
``git_control_plane_runtime`` (snapshot/evaluation/alerts/read API) and
``git_control_plane_actions`` (proposals/capabilities/trace/persistence).
This module owns the class stitching, the constructor and the event bus.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import audit_chain
from . import automation_supervisor_state
from .git_control_plane_actions import _Actions
from .git_control_plane_collectors import _Collectors
from .git_control_plane_runtime import _Runtime
from .git_control_plane_types import (
    _AUDIT_WORTHY_EVENTS,
    DEFAULT_LOCK_STALL_SECONDS,
    DEFAULT_PROPOSAL_TTL_SECONDS,
    STATE_SUBDIR,
    ActionProposal,
    CapabilityToken,
    Dimension,
    DimensionHealth,
    EventType,
    GitEvent,
    GitGlobalState,
    GitSnapshot,
    HealthStatus,
    ReasonCode,
    Severity,
    StateKind,
)

_logger_name = "gptbridge.git.control-plane"


# ---------------------------------------------------------------------------
# Control Plane
# ---------------------------------------------------------------------------


class GitControlPlane(_Actions, _Runtime, _Collectors):
    """Aggregates Git runtime state; proposes — never executes — actions.

    All collectors are dependency-injectable: pass ``sources`` mapping
    name -> callable to override ``collect_<name>`` (tests inject fakes;
    production uses the real Git / registry / queue / audit surfaces).
    """

    _COLLECTORS = (
        "state", "health", "workers", "queue", "locks",
        "audit", "remotes", "recovery", "performance",
    )

    def __init__(
        self,
        root: str | Path,
        *,
        sources: Optional[dict[str, Callable[[], dict[str, Any]]]] = None,
        state_dir: Optional[Path] = None,
        proposal_ttl: float = DEFAULT_PROPOSAL_TTL_SECONDS,
        lock_stall_seconds: float = DEFAULT_LOCK_STALL_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).resolve()
        self._now = now
        self._proposal_ttl = proposal_ttl
        self._lock_stall_seconds = lock_stall_seconds
        self._lock = threading.Lock()
        self._generation = 0
        self._fast_snapshot: Optional[GitSnapshot] = None
        self._events: list[GitEvent] = []
        self._event_sink: Optional[Callable[[GitEvent], None]] = None
        self._alerts: dict[str, dict[str, Any]] = {}
        self._decisions: list[dict[str, Any]] = []
        self._proposals: dict[str, ActionProposal] = {}
        self._capabilities: dict[str, CapabilityToken] = {}
        self._commands: list[dict[str, Any]] = []
        self._heartbeats: dict[str, dict[str, Any]] = {}
        self._prev_dimension_status: dict[str, str] = {}
        self._state_dir = state_dir or (
            automation_supervisor_state._state_dir(self.root).parent
            / STATE_SUBDIR
        )
        self._sources: dict[str, Callable[[], dict[str, Any]]] = {}
        for name in self._COLLECTORS:
            override = (sources or {}).get(name)
            self._sources[name] = override or getattr(self, f"_collect_{name}")

    # -- event bus ----------------------------------------------------------

    def emit(self, event: GitEvent) -> GitEvent:
        """Append an event.  Events invalidate fast state; they never
        replace verified Git state (§168)."""
        self._events.append(event)
        self._fast_snapshot = None  # event -> cache invalidation
        if self._event_sink is not None:
            self._event_sink(event)
        if event.event_type in _AUDIT_WORTHY_EVENTS:
            try:
                audit_chain.append_audit({
                    "operation": "control-plane-event",
                    "event": event.to_dict(),
                })
            except Exception:
                pass
        return event

    def set_event_sink(self, sink: Callable[[GitEvent], None]) -> None:
        self._event_sink = sink

    def events(
        self, *, event_type: Optional[EventType] = None,
        transaction_id: str = "", limit: int = 200,
    ) -> list[GitEvent]:
        out = self._events
        if event_type is not None:
            out = [e for e in out if e.event_type is event_type]
        if transaction_id:
            out = [e for e in out if e.transaction_id == transaction_id]
        return out[-limit:]


__all__ = [
    "ActionProposal", "CapabilityToken", "Dimension", "DimensionHealth",
    "EventType", "GitControlPlane", "GitEvent", "GitGlobalState",
    "GitSnapshot", "HealthStatus", "ReasonCode", "Severity", "StateKind",
]
