"""Maintenance Sovereign — capability / installation detection mixin.

Extracted from ``maintenance_sovereign`` (retired, A302/A323) to keep each module focused and
under 500 lines.  Read-only detection only — never installs or remediates.
Per A125/E102, the maintenance sovereign monitors system health; this mixin
detects whether maintenance-relevant functions/components are present.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

from shared_layer.service_probe import probe_registered_local_service

from .sovereign_utils import _iso_now


class MaintenanceCapabilityMixin:
    """Capability / installation detection (read-only, independent schedule).

    Expects the following attributes to be set by the composing class's
    ``__init__``:

      * ``self.app`` — the GPTBridgeApp instance
      * ``self._daily_cleaner`` / ``self._repair_service`` / ``self._hot_update``
      * ``self._health_checker``
      * ``self._capability_task`` / ``self._capability_interval_seconds``
      * ``self._capability_report``
      * ``self._started``
    """

    # ------------------------------------------------------------------
    # Capability / installation detection (read-only, independent schedule)
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_service(service: str) -> bool:
        return probe_registered_local_service(service, timeout=0.5).reachable

    def _run_capability_checks(self) -> dict[str, Any]:
        """Detect whether maintenance-relevant functions/components are present.

        Detection only — per project policy nothing is installed or remediated
        here; missing components are simply reported.
        """

        root = Path(getattr(self.app, "project_root", ".") or ".").resolve()

        functions: dict[str, bool] = {
            "health_checker": callable(self._health_checker),
            "daily_cleaner": self._daily_cleaner is not None,
            "automatic_repair": self._repair_service is not None,
            "hot_update": self._hot_update is not None,
            "resource_release": callable(
                getattr(self.app, "resource_release", None)
            ),
            "governance": getattr(self.app, "governance", None) is not None,
        }

        components: dict[str, Any] = {
            "python": {"installed": bool(sys.executable)},
            "node": {"installed": shutil.which("node") is not None},
            "npm": {
                "installed": shutil.which("npm") is not None
                or shutil.which("npm.cmd") is not None
            },
            "git": {"installed": shutil.which("git") is not None},
            "ollama": {
                "installed": shutil.which("ollama") is not None,
                "reachable": self._probe_service("ollama"),
            },
            "postgresql": {
                "installed": shutil.which("psql") is not None
                or shutil.which("pg_isready") is not None,
                "reachable": self._probe_service("postgresql"),
            },
            "qdrant": {
                "installed": (
                    root / "Standalone tools" / "local-model" / "runtime" / "qdrant"
                ).is_dir(),
                "reachable": self._probe_service("qdrant"),
            },
            "shared_layer_data": {
                "installed": (root / "shared-layer" / "data").is_dir()
            },
        }

        missing_functions = sorted(
            name for name, ok in functions.items() if not ok
        )
        missing_components = sorted(
            name
            for name, entry in components.items()
            if isinstance(entry, dict) and not entry.get("installed")
        )
        unreachable = sorted(
            name
            for name, entry in components.items()
            if isinstance(entry, dict)
            and "reachable" in entry
            and not entry.get("reachable")
        )
        ok = not missing_functions and not missing_components
        return {
            "ok": ok,
            "status": "healthy" if ok else "degraded",
            "checked_at": _iso_now(),
            "interval_seconds": self._capability_interval_seconds,
            "mode": "read-only-detection-no-auto-install",
            "functions": functions,
            "missing_functions": missing_functions,
            "components": components,
            "missing_components": missing_components,
            "unreachable_services": unreachable,
        }

    def _capability_status(self) -> dict[str, Any]:
        report = self._capability_report
        if report is not None:
            return dict(report)
        return {
            "ok": None,
            "status": "pending-first-check",
            "mode": "read-only-detection-no-auto-install",
            "interval_seconds": self._capability_interval_seconds,
        }

    async def _capability_check_loop(self) -> None:
        while not self._stop_requested():
            try:
                self._capability_report = await asyncio.to_thread(
                    self._run_capability_checks
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._capability_report = {
                    "ok": False,
                    "status": "check-failed",
                    "checked_at": _iso_now(),
                    "mode": "read-only-detection-no-auto-install",
                    "error": f"{type(error).__name__}: {error}",
                }
            try:
                await asyncio.sleep(self._capability_interval_seconds)
            except asyncio.CancelledError:
                raise
