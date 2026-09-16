"""Update health monitor checks mixin (A185 split).

Contains the _check_governance_audit and _check_update_channel methods
extracted from UpdateHealthMonitor.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core_system.update_manager_types import HealthCheckResult


class UpdateHealthChecksMixin:
    """Governance audit and update channel health checks."""

    app: Any
    project_root: Path

    def _check_governance_audit(self) -> HealthCheckResult:
        """Verify the governance audit passes."""
        try:
            project_root = self.project_root
            result = subprocess.run(
                [
                    sys.executable, "-m",
                    "governance_rule.execution.audit",
                ],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=15.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            output = (result.stdout or "") + (result.stderr or "")
            passed = "[PASS]" in output and result.returncode == 0
            if passed:
                return HealthCheckResult(
                    "governance_audit", True, "Governance audit passed",
                    datetime.now(timezone.utc).isoformat(), 0
                )
            return HealthCheckResult(
                "governance_audit", False,
                f"Governance audit failed: {output.strip()[:200]}",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except subprocess.TimeoutExpired:
            return HealthCheckResult(
                "governance_audit", False, "Governance audit timed out",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except Exception as e:
            return HealthCheckResult(
                "governance_audit", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )

    def _check_update_channel(self) -> HealthCheckResult:
        """Verify the update delivery channel is healthy."""
        try:
            errors = []

            # Check IPC connection
            ipc_ok = False
            try:
                if hasattr(self.app, "_active_ui_shells") and self.app._active_ui_shells:
                    ipc_ok = True
            except Exception:
                pass
            if not ipc_ok:
                errors.append("IPC: no active connections")

            # Check governance
            gov_ok = False
            try:
                governance = getattr(self.app, "governance", None)
                if governance is not None and hasattr(governance, "runtime_integrity_ready"):
                    gov_ok = governance.runtime_integrity_ready(max_age_seconds=5)
            except Exception:
                pass
            if not gov_ok:
                errors.append("Governance unreachable")

            # Check decision sovereign
            dec_ok = hasattr(self.app, "decision_sovereign") and self.app.decision_sovereign is not None
            if not dec_ok:
                errors.append("Decision sovereign missing")

            # Check automation sovereign
            auto_ok = hasattr(self.app, "automation_sovereign") and self.app.automation_sovereign is not None
            if not auto_ok:
                errors.append("Automation sovereign missing")

            # Check hot reload watcher
            watcher_ok = hasattr(self.app, "hot_reload_watcher") and self.app.hot_reload_watcher is not None
            if not watcher_ok:
                errors.append("Hot reload watcher missing")

            # Check UpdateManager circuit breaker (if available)
            cb_ok = True
            try:
                update_manager = getattr(self.app, "update_manager", None)
                if update_manager and hasattr(update_manager, "_circuit_breaker"):
                    cb_ok = update_manager._circuit_breaker.is_available()
            except Exception:
                pass
            if not cb_ok:
                errors.append("Update circuit breaker open")

            if errors:
                return HealthCheckResult(
                    "update_channel", False,
                    f"Channel degraded: {'; '.join(errors)}",
                    datetime.now(timezone.utc).isoformat(), 0
                )

            return HealthCheckResult(
                "update_channel", True, "Update channel healthy",
                datetime.now(timezone.utc).isoformat(), 0
            )

        except Exception as e:
            return HealthCheckResult(
                "update_channel", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )


__all__ = ["UpdateHealthChecksMixin"]
