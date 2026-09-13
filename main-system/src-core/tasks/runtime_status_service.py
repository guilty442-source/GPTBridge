from __future__ import annotations

import time
from typing import Any


_STATUS_CACHE_TTL_SECONDS: float = 1.0
_status_cache: dict[int, tuple[float, dict[str, Any]]] = {}


class RuntimeStatusService:
    """Read-only status for the main program.

    Cleanup, diagnosis, repair, quarantine, and recovery belong to the
    main-system central repair service (``tasks.central_repair``).
    """

    COMMANDS = {"app:get-runtime-status"}

    def __init__(self, app: Any) -> None:
        self.app = app

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def handle(
        self,
        command: str,
        _payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if command == "app:get-runtime-status":
            return "app:get-runtime-status_result", self.startup_status()
        raise ValueError(f"Unknown runtime status command: {command}")

    def startup_status(self) -> dict[str, Any]:
        """Build the full runtime status payload (cached for one second).

        The UI requests this command continuously, and every build
        aggregates all sovereign status trees, integrity checks, and
        dependency probes on the IPC event loop.  A short TTL collapses
        the duplicated work while keeping the surface current enough for
        status display; callers receive their own top-level mapping so a
        consumer cannot pollute the cached snapshot.
        """
        now = time.monotonic()
        cache_key = id(self.app)
        cached = _status_cache.get(cache_key)
        if cached is not None and now - cached[0] < _STATUS_CACHE_TTL_SECONDS:
            return dict(cached[1])
        from .readiness_gate import ReadinessGate

        readiness = ReadinessGate(self.app).evaluate()
        result: dict[str, Any] = {
            "ok": readiness.overall_ready,
            "backend": readiness.runtime_state,
            "version": str(getattr(self.app, "version", "0.0.0")),
            "runtime_scope": getattr(
                getattr(self.app, "command_router", None), "scope", "starting"
            ),
            "message": "runtime status ok",
            **readiness.as_dict(),
        }
        get_startup_status = getattr(self.app, "get_startup_status", None)
        if callable(get_startup_status):
            result.update(get_startup_status())
        _status_cache[cache_key] = (now, result)
        return dict(result)
