"""Hot-reload watcher — Reload Execution and Terminal Polling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hot_reload_watcher_constants import FAILURE_BACKOFF_SECONDS


class ReloadExecutionMixin:
    """Reload execution and decision-sovereign feedback."""

    _in_flight: bool
    app: Any
    project_root: Path

    async def _execute_reload(
        self,
        module_names: list[str],
        token_path: str,
    ) -> bool:
        """Execute the reload via hot-update service."""
        self._in_flight = True
        try:
            app = self.app
            hot_update = getattr(app, "hot_update_service", None)
            prepare = getattr(hot_update, "prepare_generation", None)
            if not callable(prepare):
                return False
            prepared = await asyncio.to_thread(
                prepare,
                governance=app.governance,
                modules=module_names,
                standby_validation=True,
            )
            if not bool(getattr(prepared, "ok", False)):
                self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
                return False

            return await self._submit_update_request(module_names, prepared, token_path)
        except Exception as error:
            self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
            self._log({"type": "hot_reload_watcher", "ok": False,
                       "error": f"{type(error).__name__}: {error}",
                       "modules": module_names})
            return False
        finally:
            self._in_flight = False

    async def _submit_update_request(
        self,
        module_names: list[str],
        prepared: Any,
        token_path: str,
    ) -> bool:
        """Submit update request and poll for terminal status."""
        operation_id = self._generate_operation_id()
        target_generation = self._generate_target_generation()
        request_path = self._get_request_path()

        payload = self._build_update_payload(
            operation_id, target_generation, token_path, module_names, prepared
        )
        self._write_request_file(request_path, payload)

        self._record_reload_metrics()
        self._log_reload_start(operation_id, target_generation, module_names)
        self._report_to_decision_sovereign(
            operation_id=operation_id,
            terminal_status="prepared",
            modules=module_names,
            target_generation=target_generation,
        )

        self._start_terminal_poll(operation_id, request_path)
        return True

    def _generate_operation_id(self) -> str:
        return uuid.uuid4().hex

    def _generate_target_generation(self) -> str:
        return f"backend-{time.time_ns()}"

    def _get_request_path(self) -> Path:
        path = (
            self.project_root / "main-system" / "runtime" / "state"
            / "backend-update-request.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _build_update_payload(
        self,
        operation_id: str,
        target_generation: str,
        token_path: str,
        module_names: list[str],
        prepared: Any,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation_id": operation_id,
            "update_type": "backend-release",
            "certified": True,
            "decision_owner": "automation-sovereign",
            "decision_basis": "A330",
            "permission_scope": token_path,
            "modules": module_names,
            "artifact_hashes": dict(prepared.hashes),
            "target_generation": target_generation,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "terminal_status": "prepared",
        }

    def _write_request_file(self, request_path: Path, payload: dict[str, Any]) -> None:
        temporary = request_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, request_path)

    def _record_reload_metrics(self) -> None:
        self._last_reload_at = time.monotonic()
        self._reload_timestamps.append(self._last_reload_at)

    def _log_reload_start(
        self,
        operation_id: str,
        target_generation: str,
        module_names: list[str],
    ) -> None:
        self._log({
            "type": "hot_reload_watcher",
            "ok": True,
            "modules": module_names,
            "operation_id": operation_id,
            "target_generation": target_generation,
            "handover": "prepared",
        })

    def _start_terminal_poll(self, operation_id: str, request_path: Path) -> None:
        threading.Thread(
            target=self._poll_terminal_status,
            args=(operation_id, request_path),
            daemon=True,
            name=f"hot-reload-terminal-{operation_id[:8]}",
        ).start()

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