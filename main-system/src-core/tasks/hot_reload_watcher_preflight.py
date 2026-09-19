"""Hot-reload watcher — Preflight and Confirmation."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .hot_reload_watcher_constants import (
    MIN_RELOAD_INTERVAL_SECONDS,
    MAX_RELOADS_PER_MINUTE,
    FAILURE_BACKOFF_SECONDS,
)


class PreflightConfirmationMixin:
    """Preflight checks and user-confirmation gate."""

    _in_flight: bool
    _enabled: bool
    _backoff_until: float
    _last_reload_at: float
    _reload_timestamps: list[float]
    app: Any

    def _preflight_checks(self) -> bool:
        """Run all preflight checks before reload attempt."""
        if self._in_flight or not self._enabled:
            return False
        now = time.monotonic()
        if now < self._backoff_until:
            return False
        if now - self._last_reload_at < MIN_RELOAD_INTERVAL_SECONDS:
            return False
        self._reload_timestamps = [
            stamp for stamp in self._reload_timestamps if now - stamp < 60.0
        ]
        if len(self._reload_timestamps) >= MAX_RELOADS_PER_MINUTE:
            return False

        app = self.app
        if getattr(app, "startup_dead", False) or not getattr(app, "maintenance_ready", False):
            return False
        if getattr(app, "automation_sovereign", None) is None:
            return False
        if getattr(app, "decision_sovereign", None) is None:
            return False
        if getattr(app, "governance", None) is None:
            return False
        return True

    async def _handle_user_confirmation(
        self,
        module_names: list[str],
        changed_paths: list[str],
        user_confirmed: bool,
    ) -> bool:
        """Handle user-confirmation gate. Returns True to proceed, False to wait."""
        if user_confirmed:
            return True

        from core_system.auto_action_policy import automatic_update_execution_allowed
        if automatic_update_execution_allowed():
            return True

        self._record_pending_action(module_names, changed_paths)
        self._log({
            "type": "hot_reload_watcher",
            "ok": False,
            "awaiting_user_confirmation": True,
            "modules": sorted(module_names),
        })
        return False

    def _record_pending_action(
        self,
        module_names: list[str],
        changed_paths: list[str],
    ) -> None:
        """Record pending action for user confirmation."""
        try:
            from core_system.auto_action_policy import (
                CONFIRMATION_TTL_SECONDS,
                record_pending_action,
            )

            detail = self._build_action_detail(module_names, changed_paths)
            action_id = self._generate_action_id(module_names)
            expires_at = self._compute_expiry(CONFIRMATION_TTL_SECONDS)
            binding = self._build_binding(action_id, module_names, changed_paths)

            record_pending_action(
                self.project_root,
                kind="update",
                summary=detail["summary"],
                detail=detail,
                action_id=action_id,
                binding={**binding, "expires_at": expires_at},
            )
        except Exception:
            pass

    def _build_action_detail(
        self,
        module_names: list[str],
        changed_paths: list[str],
    ) -> dict[str, Any]:
        file_evidence = self._build_file_evidence(changed_paths)
        compatibility_ok = all(
            item.get("syntax_ok") is not False for item in file_evidence
        )
        return {
            "modules": sorted(module_names),
            "changed_paths": sorted(changed_paths)[:20],
            "files": file_evidence,
            "compatibility": {
                "syntax_ok": compatibility_ok,
                "evidence": "compile check of changed sources",
            },
            "source": "backend source change",
            "version": self._active_generation_hint(),
        }

    def _generate_action_id(self, module_names: list[str]) -> str:
        import hashlib
        return (
            "update-"
            + hashlib.sha256(
                ",".join(sorted(module_names)).encode("utf-8")
            ).hexdigest()[:16]
        )

    def _compute_expiry(self, ttl_seconds: int) -> str:
        from datetime import datetime, timedelta, timezone
        return (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).isoformat()

    def _build_binding(
        self,
        action_id: str,
        module_names: list[str],
        changed_paths: list[str],
    ) -> dict[str, Any]:
        return {
            "update_id": action_id,
            "scope": ", ".join(sorted(module_names)),
            "target": sorted(changed_paths)[0] if changed_paths else "",
            "proposed_method": "A330 standby-generation handover",
            "risk": "backend generation switch",
            "rollback": (
                "gateway keeps the previous generation for a "
                "bounded rollback window"
            ),
        }

    def _build_file_evidence(self, changed_paths: list[str]) -> list[dict[str, Any]]:
        """Build file evidence for pending action."""
        import ast
        import hashlib
        file_evidence: list[dict[str, Any]] = []
        for raw_path in sorted(changed_paths)[:20]:
            path = Path(raw_path)
            entry: dict[str, Any] = {"path": str(path)}
            try:
                content = path.read_bytes()
                entry["sha256"] = hashlib.sha256(content).hexdigest()
                if path.suffix == ".py":
                    try:
                        ast.parse(content.decode("utf-8"))
                        entry["syntax_ok"] = True
                    except (SyntaxError, UnicodeDecodeError):
                        entry["syntax_ok"] = False
                else:
                    entry["syntax_ok"] = None
            except OSError:
                entry["sha256"] = ""
                entry["syntax_ok"] = None
            file_evidence.append(entry)
        return file_evidence