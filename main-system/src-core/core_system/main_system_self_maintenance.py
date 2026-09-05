"""Main-system self-maintenance service.

Per the Governance Codex the Maintenance Sovereign owns ALL system-maintenance
matters.  This service is the governed executor for the main system's OWN
self-maintenance: it runs three bounded, main-system-local duties and reports
back to the sovereign.  It never touches another module, never crosses the
governance boundary, and never performs cross-tool repair.

Duties (all main-system-local, all read-only or main-system-rooted):
  1. source self-repair — `SourceRepairService` over `main-system/src-core`
  2. local cleanup       — `run_local_cleanup` over `main-system/`
  3. integrity verify    — `GovernanceAuthenticationService.verify_runtime_integrity`

Triggers:
  * startup  — one run right after the Maintenance Sovereign starts
  * periodic — every `interval_seconds` (default 6h, env-overridable)
  * manual   — IPC command `app:run-main-system-self-maintenance`

The service is fail-safe: any single duty failure is recorded and the next
duty still runs; the overall report aggregates per-duty results.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from governance_rule.execution.tool_runtime.tool_local_cleanup import (
    run_local_cleanup,
    write_local_cleanup_state,
)

MAIN_SYSTEM_TOOL_ID: Final[str] = "main-system"
DEFAULT_INTERVAL_SECONDS: Final[float] = 6 * 60 * 60
MIN_INTERVAL_SECONDS: Final[float] = 60.0
SELF_MAINTENANCE_VERSION: Final[str] = "1.0.0"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MainSystemSelfMaintenance:
    """Bounded self-maintenance for the main system itself.

    The service is constructed with a project root and an optional governance
    authentication service (used only for the read-only integrity verify
    duty).  The heavy repair/cleanup code runs in a worker thread via
    ``asyncio.to_thread`` so the mother process stays responsive.
    """

    VERSION = SELF_MAINTENANCE_VERSION

    def __init__(
        self,
        project_root: Path,
        *,
        authentication: Any = None,
        interval_seconds: float | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.authentication = authentication
        env_interval = os.environ.get("GPTBRIDGE_MAIN_SELF_MAINTENANCE_INTERVAL")
        try:
            env_value = float(env_interval) if env_interval else None
        except ValueError:
            env_value = None
        chosen = (
            interval_seconds
            if interval_seconds is not None
            else env_value
            if env_value is not None
            else DEFAULT_INTERVAL_SECONDS
        )
        self.interval_seconds = max(float(chosen), MIN_INTERVAL_SECONDS)
        self._loop_task: asyncio.Task[Any] | None = None
        self._last_report: dict[str, Any] | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Run the startup pass and schedule the periodic loop."""

        if self._running:
            return self.status()
        self._running = True
        # Startup pass — fail-safe, never aborts the loop on a duty error.
        self._last_report = await self._run_all_duties()
        self._loop_task = asyncio.create_task(
            self._periodic_loop(),
            name="main-system-self-maintenance",
        )
        return {
            "ok": True,
            "role": "main-system-self-maintenance",
            "started_at": _iso_now(),
            "interval_seconds": self.interval_seconds,
            "startup_report": self._last_report,
        }

    async def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None

    async def run_once(self) -> dict[str, Any]:
        """Manual trigger — runs all duties once and returns the report."""

        report = await self._run_all_duties()
        self._last_report = report
        return report

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "role": "main-system-self-maintenance",
            "version": self.VERSION,
            "enabled": self._running,
            "interval_seconds": self.interval_seconds,
            "loop_running": (
                self._loop_task is not None and not self._loop_task.done()
            ),
            "last_report": self._last_report,
        }

    # ------------------------------------------------------------------
    # Duties
    # ------------------------------------------------------------------

    async def _run_all_duties(self) -> dict[str, Any]:
        started_at = _iso_now()
        source_report = await self._duty_source_self_repair()
        cleanup_report = await self._duty_local_cleanup()
        integrity_report = await self._duty_integrity_verify()
        ok = all(
            bool(item.get("ok"))
            for item in (source_report, cleanup_report, integrity_report)
        )
        return {
            "ok": ok,
            "operation": "main-system-self-maintenance",
            "authority": "maintenance-sovereign",
            "version": self.VERSION,
            "started_at": started_at,
            "completed_at": _iso_now(),
            "duties": {
                "source_self_repair": source_report,
                "local_cleanup": cleanup_report,
                "integrity_verify": integrity_report,
            },
        }

    async def _duty_source_self_repair(self) -> dict[str, Any]:
        try:
            from tasks.source_repair import self_repair_sources
        except Exception as error:
            return {
                "ok": False,
                "duty": "source-self-repair",
                "error": f"{type(error).__name__}: {error}",
            }
        try:
            report = await asyncio.to_thread(
                self_repair_sources, self.project_root, record=True
            )
        except Exception as error:
            return {
                "ok": False,
                "duty": "source-self-repair",
                "error": f"{type(error).__name__}: {error}",
            }
        return {
            "ok": bool(report.get("ok")) or not report.get("errors"),
            "duty": "source-self-repair",
            "probed_sources": report.get("probed_sources"),
            "repaired_files": report.get("repaired_files"),
            "ambiguous_files": report.get("ambiguous_files"),
            "errors": report.get("errors"),
            "recorded_run": report.get("recorded_run"),
        }

    async def _duty_local_cleanup(self) -> dict[str, Any]:
        tool_root = self.project_root / MAIN_SYSTEM_TOOL_ID
        try:
            result = await asyncio.to_thread(
                run_local_cleanup, MAIN_SYSTEM_TOOL_ID, tool_root
            )
        except Exception as error:
            return {
                "ok": False,
                "duty": "local-cleanup",
                "error": f"{type(error).__name__}: {error}",
            }
        with _suppress(Exception):
            await asyncio.to_thread(
                write_local_cleanup_state, tool_root, result
            )
        return {
            "ok": bool(result.get("ok")),
            "duty": "local-cleanup",
            "cleaned_files": result.get("cleaned_files"),
            "cleaned_directories": result.get("cleaned_directories"),
            "cleaned_bytes": result.get("cleaned_bytes"),
            "skipped": result.get("skipped"),
        }

    async def _duty_integrity_verify(self) -> dict[str, Any]:
        auth = self.authentication
        if auth is None:
            governance = getattr(_app_proxy, "governance", None)
            auth = getattr(governance, "authentication", None) if governance else None
        if auth is None or not hasattr(auth, "verify_runtime_integrity"):
            return {
                "ok": True,
                "duty": "integrity-verify",
                "skipped": True,
                "reason": "authentication-service-unavailable",
            }
        try:
            await asyncio.to_thread(auth.verify_runtime_integrity)
        except PermissionError as error:
            return {
                "ok": False,
                "duty": "integrity-verify",
                "error": f"PERMISSION_DENIED: {error}",
            }
        except Exception as error:
            return {
                "ok": False,
                "duty": "integrity-verify",
                "error": f"{type(error).__name__}: {error}",
            }
        return {"ok": True, "duty": "integrity-verify", "verified": True}

    async def _periodic_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            if not self._running:
                return
            try:
                self._last_report = await self._run_all_duties()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._last_report = {
                    "ok": False,
                    "operation": "main-system-self-maintenance",
                    "error": f"{type(error).__name__}: {error}",
                    "completed_at": _iso_now(),
                }


# Late-bound app proxy used only for the integrity duty's fallback lookup.
# Set via ``bind_app`` from the mother process; never imported at module load.
_app_proxy: Any = None


def bind_app(app: Any) -> None:
    global _app_proxy
    _app_proxy = app


def _suppress(*exceptions: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*exceptions)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "MAIN_SYSTEM_TOOL_ID",
    "MainSystemSelfMaintenance",
    "bind_app",
]
