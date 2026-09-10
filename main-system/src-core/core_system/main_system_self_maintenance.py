"""Main-system self-maintenance service.

Per the Governance Codex the Maintenance Sovereign owns ALL system-maintenance
matters.  This service is the governed executor for the main system's OWN
self-maintenance: it runs two bounded, main-system-local duties and reports
back to the sovereign.  It never touches another module, never crosses the
governance boundary, and never performs cross-tool repair.

Duties (all main-system-local, all read-only or main-system-rooted):
  1. source self-repair — `SourceRepairService` over `main-system/src-core`
  2. integrity verify    — `GovernanceAuthenticationService.verify_runtime_integrity`

Local cleanup is exclusively scheduled by ``DailyGlobalCleanerService``.

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
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

from .sovereign_utils import _iso_now, _suppress
from .versioning import component_version

MAIN_SYSTEM_TOOL_ID: Final[str] = "main-system"
DEFAULT_INTERVAL_SECONDS: Final[float] = 6 * 60 * 60
MIN_INTERVAL_SECONDS: Final[float] = 60.0
SELF_MAINTENANCE_VERSION: Final[str] = component_version("main-system-self-maintenance")


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
        # The startup pass explicitly runs version compatibility and source
        # stability repair; runtime status changes stay deferred until it ends.
        self._last_report = await self._run_all_duties(startup=True)
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

    async def _run_all_duties(self, startup: bool = False) -> dict[str, Any]:
        started_at = _iso_now()
        # A181/E156: resolve active release pointer before any repair or
        # integrity check.  The active certified release is the only
        # baseline; startup must reject any older default overwrite.
        from .active_release import resolve_active_pointer

        active_pointer = resolve_active_pointer()
        version_report = await self._duty_version_compatibility()
        # Stability fix runs only during the startup pass; the periodic loop
        # reports it as skipped to avoid mutating sources after every interval.
        stability_report = await self._duty_stability_fix(startup=startup)
        integrity_report = await self._duty_integrity_verify()
        ok = all(
            bool(item.get("ok"))
            for item in (version_report, stability_report, integrity_report)
        )
        return {
            "ok": ok,
            "operation": "main-system-self-maintenance",
            "authority": "maintenance-sovereign",
            "version": self.VERSION,
            "started_at": started_at,
            "completed_at": _iso_now(),
            "active_release_id": active_pointer.release_id if active_pointer else "",
            "active_release_baseline": "A181/E156",
            "duties": {
                "version_compatibility": version_report,
                "stability_fix": stability_report,
                "local_cleanup": {
                    "ok": True,
                    "skipped": True,
                    "reason": "DAILY_GLOBAL_CLEANER_OWNS_SCHEDULE",
                    "delegated_to": "daily-global-cleaner",
                },
                "integrity_verify": integrity_report,
            },
        }

    async def _duty_version_compatibility(self) -> dict[str, Any]:
        script = self.project_root / "main-system" / "scripts" / "sync_version.py"
        if not script.is_file():
            return {
                "ok": True,
                "duty": "version-compatibility",
                "skipped": True,
                "reason": "sync_version.py-not-found",
            }
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(script), "check"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "duty": "version-compatibility",
                "error": "version-compatibility-check-timeout",
            }
        except Exception as error:
            return {
                "ok": False,
                "duty": "version-compatibility",
                "error": f"{type(error).__name__}: {error}",
            }
        return {
            "ok": result.returncode == 0,
            "duty": "version-compatibility",
            "returncode": result.returncode,
            "stdout": result.stdout.strip() if result.stdout else "",
            "stderr": result.stderr.strip() if result.stderr else "",
        }

    async def _duty_stability_fix(self, startup: bool = False) -> dict[str, Any]:
        if not startup:
            return {
                "ok": True,
                "duty": "stability-fix",
                "skipped": True,
                "reason": "stability-fix-runs-only-at-startup",
            }
        try:
            from tasks.source_repair import SourceRepairService, syntax_problems
        except Exception as error:
            return {
                "ok": False,
                "duty": "stability-fix",
                "error": f"{type(error).__name__}: {error}",
            }

        def _check() -> dict[str, Any]:
            service = SourceRepairService(self.project_root)
            problems: list[dict[str, Any]] = []
            try:
                sources = service.python_sources()
            except Exception as probe_error:
                return {
                    "ok": False,
                    "probed_sources": 0,
                    "problems": problems,
                    "error": f"{type(probe_error).__name__}: {probe_error}",
                }
            for source_path in sources:
                problem = syntax_problems(source_path)
                if not problem.get("ok"):
                    problems.append(
                        {
                            "file": str(
                                source_path.relative_to(self.project_root).as_posix()
                            ),
                            "error": problem.get("error"),
                            "message": problem.get("message"),
                        }
                    )
            return {
                "ok": len(problems) == 0,
                "probed_sources": len(sources),
                "problems": problems,
            }

        try:
            report = await asyncio.to_thread(_check)
        except Exception as error:
            return {
                "ok": False,
                "duty": "stability-fix",
                "error": f"{type(error).__name__}: {error}",
            }
        return {
            "ok": report.get("ok"),
            "duty": "stability-fix",
            "probed_sources": report.get("probed_sources"),
            "problems": report.get("problems"),
            "error": report.get("error"),
        }

    async def _duty_integrity_verify(self) -> dict[str, Any]:
        auth = self.authentication
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
                self._last_report = await self._run_all_duties(startup=False)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._last_report = {
                    "ok": False,
                    "operation": "main-system-self-maintenance",
                    "error": f"{type(error).__name__}: {error}",
                    "completed_at": _iso_now(),
                }


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "MAIN_SYSTEM_TOOL_ID",
    "MainSystemSelfMaintenance",
]
