"""Hot-reload watcher reload request mixin.

Provides the _maybe_reload method and decision-sovereign feedback
loop for the HotReloadWatcher class.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .hot_reload_watcher_constants import (
    MIN_RELOAD_INTERVAL_SECONDS,
    MAX_RELOADS_PER_MINUTE,
    FAILURE_BACKOFF_SECONDS,
)


class HotReloadReloadMixin:
    """Reload request and decision-sovereign feedback methods for HotReloadWatcher."""

    async def _maybe_reload(
        self,
        changed_paths: list[str],
        *,
        user_confirmed: bool = False,
    ) -> bool:
        """Attempt one reload; return whether the pending revision was consumed."""
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
        if getattr(app, "startup_dead", False) or not getattr(
            app, "maintenance_ready", False
        ):
            return False
        synchronization = getattr(app, "synchronization_sovereign", None)
        if synchronization is None:
            return False
        decision_sovereign = getattr(app, "decision_sovereign", None)
        if decision_sovereign is None:
            return False
        governance = getattr(app, "governance", None)
        if governance is None:
            return False

        module_names = self._loaded_module_names(changed_paths)
        if not module_names:
            return True

        # User-confirmation gate
        from core_system.auto_action_policy import (
            automatic_update_execution_allowed,
            record_pending_action,
        )

        if not automatic_update_execution_allowed() and not user_confirmed:
            try:
                from core_system.auto_action_policy import (
                    CONFIRMATION_TTL_SECONDS,
                )

                summary = "backend update: " + ", ".join(sorted(module_names))
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
                compatibility_ok = all(
                    item.get("syntax_ok") is not False for item in file_evidence
                )
                expires_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=CONFIRMATION_TTL_SECONDS)
                ).isoformat()
                action_id = (
                    "update-"
                    + hashlib.sha256(
                        ",".join(sorted(module_names)).encode("utf-8")
                    ).hexdigest()[:16]
                )
                record_pending_action(
                    self.project_root,
                    kind="update",
                    summary=summary,
                    detail={
                        "modules": sorted(module_names),
                        "changed_paths": sorted(changed_paths)[:20],
                        "files": file_evidence,
                        "compatibility": {
                            "syntax_ok": compatibility_ok,
                            "evidence": "compile check of changed sources",
                        },
                        "source": "backend source change",
                        "version": self._active_generation_hint(),
                    },
                    action_id=action_id,
                    binding={
                        "update_id": action_id,
                        "scope": ", ".join(sorted(module_names)),
                        "target": sorted(changed_paths)[0]
                        if changed_paths
                        else "",
                        "proposed_method": "A330 standby-generation handover",
                        "risk": "backend generation switch",
                        "rollback": (
                            "gateway keeps the previous generation for a "
                            "bounded rollback window"
                        ),
                        "expires_at": expires_at,
                    },
                )
            except Exception:
                pass
            self._log({
                "type": "hot_reload_watcher",
                "ok": False,
                "awaiting_user_confirmation": True,
                "modules": sorted(module_names),
            })
            return True

        token_path = self._token_resource_path(changed_paths)
        if token_path is None:
            return True
        self._in_flight = True
        try:
            hot_update = getattr(app, "hot_update_service", None)
            prepare = getattr(hot_update, "prepare_generation", None)
            if not callable(prepare):
                return False
            prepared = await asyncio.to_thread(
                prepare,
                governance=governance,
                modules=module_names,
                standby_validation=True,
            )
            if not bool(getattr(prepared, "ok", False)):
                self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
                return False
            operation_id = uuid.uuid4().hex
            target_generation = f"backend-{time.time_ns()}"
            request_path = (
                self.project_root / "main-system" / "runtime" / "state"
                / "backend-update-request.json"
            )
            request_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "operation_id": operation_id,
                "update_type": "backend-release",
                "certified": True,
                "decision_owner": "synchronization-sovereign",
                "decision_basis": "A330",
                "permission_scope": token_path,
                "modules": module_names,
                "artifact_hashes": dict(prepared.hashes),
                "target_generation": target_generation,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "terminal_status": "prepared",
            }
            temporary = request_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, request_path)
            ok = True
            if ok:
                self._last_reload_at = time.monotonic()
                self._reload_timestamps.append(self._last_reload_at)
            else:
                self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
            self._log({
                "type": "hot_reload_watcher",
                "ok": ok,
                "modules": module_names,
                "operation_id": operation_id,
                "target_generation": target_generation,
                "handover": "prepared",
            })
            self._report_to_decision_sovereign(
                operation_id=operation_id,
                terminal_status="prepared",
                modules=module_names,
                target_generation=target_generation,
            )
            threading.Thread(
                target=self._poll_terminal_status,
                args=(operation_id, request_path),
                daemon=True,
                name=f"hot-reload-terminal-{operation_id[:8]}",
            ).start()
            return ok
        except Exception as error:
            self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
            self._log({"type": "hot_reload_watcher", "ok": False,
                       "error": f"{type(error).__name__}: {error}",
                       "modules": module_names})
            return False
        finally:
            self._in_flight = False

    def _report_to_decision_sovereign(
        self,
        *,
        operation_id: str,
        terminal_status: str,
        **detail: Any,
    ) -> None:
        """Report a certified-update lifecycle event to the decision-sovereign."""
        decision_sovereign = getattr(self.app, "decision_sovereign", None)
        if decision_sovereign is None:
            return
        reporter = getattr(decision_sovereign, "record_certified_update_status", None)
        if not callable(reporter):
            return
        try:
            reporter(operation_id, terminal_status, **detail)
        except Exception:
            pass

    def _poll_terminal_status(
        self,
        operation_id: str,
        request_path: Path,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Poll backend-update-request.json for a terminal status."""
        terminal_states = {
            "global-success",
            "failed-isolated",
            "rolled-back",
            "partial-deferred",
        }
        deadline = time.monotonic() + timeout
        last_status = ""
        while time.monotonic() < deadline:
            try:
                data = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                time.sleep(poll_interval)
                continue
            status = str(data.get("terminal_status") or "")
            if status == last_status:
                time.sleep(poll_interval)
                continue
            last_status = status
            if status in terminal_states and data.get("operation_id") == operation_id:
                self._report_to_decision_sovereign(
                    operation_id=operation_id,
                    terminal_status=status,
                    active_generation=data.get("active_generation", ""),
                    active_backend_port=data.get("active_backend_port"),
                    error=data.get("error", ""),
                    processed_at=data.get("processed_at", ""),
                )
                self._log({
                    "type": "hot_reload_watcher",
                    "ok": status == "global-success",
                    "operation_id": operation_id,
                    "terminal_status": status,
                })
                return
            time.sleep(poll_interval)
        self._report_to_decision_sovereign(
            operation_id=operation_id,
            terminal_status="failed-isolated",
            error="terminal-status-timeout",
        )
