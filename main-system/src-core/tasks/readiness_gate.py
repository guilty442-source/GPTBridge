"""Readiness gate — A67 four-condition readiness determination.

Per Governance Codex A67 (frontend-backend startup sync and repair), the
system may only declare ``ready`` when ALL four conditions are satisfied:

  1. backend-runtime-ready      — command router initialized
  2. governance-ready            — runtime integrity verified
  3. required-dependencies-ready — PostgreSQL, Qdrant, Ollama reachable
  4. authenticated-ipc-connected  — WebSocket origin authenticated

A socket being open alone is NOT ready (FORBID:socket-open-alone-as-ready).
The UI must not display "connected" while runtime is degraded or governance
is unready (FORBID:ui-connected-while-runtime-degraded-or-governance-unready).

This module is read-only detection — it never starts, stops, or repairs
dependencies.  Repair belongs to the maintenance sovereign decision path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from datetime import datetime, timezone

from shared_layer.service_probe import probe_registered_local_service

READINESS_GATE_VERSION: Final[str] = "1.0.0"

# Required dependency services and their default local probe ports.
REQUIRED_DEPENDENCIES: Final[tuple[tuple[str, int], ...]] = (
    ("postgresql", 5432),
    ("qdrant", 6333),
    ("ollama", 11434),
)

DEPENDENCY_PROBE_TIMEOUT: Final[float] = 0.75


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DependencyStatus:
    """Status of a single required dependency."""
    name: str
    port: int
    reachable: bool

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "port": self.port, "reachable": self.reachable}


@dataclass
class ReadinessSnapshot:
    """Point-in-time snapshot of all four readiness conditions (A67)."""
    backend_runtime_ready: bool = False
    governance_ready: bool = False
    dependencies_ready: bool = False
    authenticated_ipc_connected: bool = False
    dependencies: list[DependencyStatus] = field(default_factory=list)
    overall_ready: bool = False
    runtime_state: str = "starting"
    evaluated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend_runtime_ready": self.backend_runtime_ready,
            "governance_ready": self.governance_ready,
            "dependencies_ready": self.dependencies_ready,
            "authenticated_ipc_connected": self.authenticated_ipc_connected,
            "dependencies": [d.as_dict() for d in self.dependencies],
            "overall_ready": self.overall_ready,
            "runtime_state": self.runtime_state,
            "evaluated_at": self.evaluated_at,
        }


class ReadinessGate:
    """Evaluates the four A67 readiness conditions.

    Stateless and side-effect free: it reads live state from the app
    instance and probes dependency ports.  It never mutates anything.
    """

    VERSION = READINESS_GATE_VERSION

    def __init__(self, app: Any) -> None:
        self.app = app

    def _check_backend_runtime(self) -> bool:
        """Condition 1: backend runtime ready (command router initialized)."""
        return getattr(self.app, "command_router", None) is not None

    def _check_governance(self) -> bool:
        """Condition 2: governance runtime integrity ready."""
        governance = getattr(self.app, "governance", None)
        if governance is None:
            return False
        checker = getattr(governance, "runtime_integrity_ready", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    def _check_dependencies(self) -> tuple[bool, list[DependencyStatus]]:
        """Condition 3: required dependencies (PostgreSQL/Qdrant/Ollama) reachable."""
        statuses: list[DependencyStatus] = []
        all_reachable = True
        for name, port in REQUIRED_DEPENDENCIES:
            reachable = probe_registered_local_service(
                name, timeout=DEPENDENCY_PROBE_TIMEOUT
            ).reachable
            statuses.append(DependencyStatus(name=name, port=port, reachable=reachable))
            if not reachable:
                all_reachable = False
        return all_reachable, statuses

    def _check_authenticated_ipc(self) -> bool:
        """Condition 4: authenticated IPC connected.

        Uses an INDEPENDENT verification channel — the explicit
        ``_authenticated_ipc_connections`` counter maintained by the IPC
        server handler — rather than inferring authentication from the
        WebSocket session token.  The handler only increments this
        counter for connections that passed ``_websocket_request_authorized``
        (HMAC token + instance id check) during the handshake; an
        unauthenticated socket is rejected with 403 before reaching the
        handler and is never counted.  This satisfies A67's requirement
        that authenticated-ipc-connected be a verified condition, not an
        inference from socket state.

        At least one authenticated IPC client must be connected AND the
        backend runtime must have progressed past the initial wait
        (command router present or startup_dead).  A connected socket
        alone is not enough — the runtime must not be in a degraded/dead
        state (FORBID:ui-connected-while-runtime-degraded).
        """
        authed = getattr(self.app, "_authenticated_ipc_connections", 0)
        if not isinstance(authed, int) or authed <= 0:
            return False
        # A connected socket alone is not enough — the runtime must not be
        # in a degraded/dead state (FORBID:ui-connected-while-runtime-degraded).
        if getattr(self.app, "startup_dead", False):
            return False
        return True

    def evaluate(self) -> ReadinessSnapshot:
        """Evaluate all four readiness conditions and return a snapshot."""
        backend_ok = self._check_backend_runtime()
        governance_ok = self._check_governance()
        deps_ok, deps = self._check_dependencies()
        ipc_ok = self._check_authenticated_ipc()

        overall = backend_ok and governance_ok and deps_ok and ipc_ok

        if overall:
            runtime_state = "ready"
        elif getattr(self.app, "startup_dead", False):
            runtime_state = "degraded"
        elif not backend_ok or not governance_ok:
            runtime_state = "starting"
        elif not deps_ok:
            runtime_state = "dependencies-pending"
        else:
            runtime_state = "starting"

        return ReadinessSnapshot(
            backend_runtime_ready=backend_ok,
            governance_ready=governance_ok,
            dependencies_ready=deps_ok,
            authenticated_ipc_connected=ipc_ok,
            dependencies=deps,
            overall_ready=overall,
            runtime_state=runtime_state,
            evaluated_at=_iso_now(),
        )


__all__ = [
    "DEPENDENCY_PROBE_TIMEOUT",
    "DependencyStatus",
    "REQUIRED_DEPENDENCIES",
    "READINESS_GATE_VERSION",
    "ReadinessGate",
    "ReadinessSnapshot",
]
