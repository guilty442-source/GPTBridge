"""Source-UI launch, companion reconnect, activation, and background restart."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict

from managers.process_utils import terminate_process_tree

from .toolbox_constants import _background_subprocess_kwargs


class LaunchMixin:
    """Source-UI launch, companion reconnect, activation, and background restart."""

    # ------------------------------------------------------------------
    # Background restart policy / scheduling
    # ------------------------------------------------------------------

    def _background_restart_policy(self, tool_id: str) -> tuple[int, float] | None:
        try:
            manifest, _tool_dir = self._load_manifest_cached(tool_id)
            policy = manifest.get("background_service")
            if not isinstance(policy, dict) or policy.get("auto_restart") is not True:
                return None
            attempts = max(1, min(int(policy.get("max_restart_attempts") or 3), 5))
            backoff = max(1.0, min(float(policy.get("restart_backoff_seconds") or 2), 30.0))
            return attempts, backoff
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _schedule_background_restart(self, tool_id: str) -> None:
        if tool_id in self._force_closed_tool_ids:
            return
        if self._background_restart_policy(tool_id) is None:
            return
        active = self._background_restart_tasks.get(tool_id)
        if active is not None and not active.done():
            return
        task = asyncio.create_task(self._restart_background_tool(tool_id))
        self._background_restart_tasks[tool_id] = task

    async def _restart_background_tool(self, tool_id: str) -> None:
        current_task = asyncio.current_task()
        try:
            policy = self._background_restart_policy(tool_id)
            if policy is None:
                return
            maximum_attempts, base_backoff = policy
            while tool_id not in self._force_closed_tool_ids:
                attempt = self._background_restart_attempts.get(tool_id, 0) + 1
                if attempt > maximum_attempts:
                    return
                self._background_restart_attempts[tool_id] = attempt
                await asyncio.sleep(base_backoff * attempt)
                if tool_id in self._force_closed_tool_ids:
                    return
                result = await self.start_tool(
                    {
                        "tool_id": tool_id,
                        "request_id": f"managed-restart-{tool_id}-{uuid.uuid4().hex}",
                        "background": True,
                        "_managed_restart": True,
                    }
                )
                if result.get("ok") is True:
                    await self._reconnect_companion_source_uis(tool_id)
                    return
        finally:
            if self._background_restart_tasks.get(tool_id) is current_task:
                self._background_restart_tasks.pop(tool_id, None)

    async def _reset_restart_attempts_after_stability(
        self,
        tool_id: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        await asyncio.sleep(60)
        if process.returncode is None:
            self._background_restart_attempts.pop(tool_id, None)

    # ------------------------------------------------------------------
    # Source-UI launch
    # ------------------------------------------------------------------

    async def _launch_source_ui(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        runtime_environment: dict[str, str],
        *,
        runtime_tool_id: str | None = None,
    ) -> dict[str, Any]:
        expected_runtime_tool_id = runtime_tool_id or tool_id
        try:
            runtime_port = int(runtime_environment["GPTBRIDGE_IPC_PORT"])
        except (KeyError, TypeError, ValueError):
            runtime_port = 0

        def source_runtime_ready() -> bool:
            if not 1024 <= runtime_port <= 65535:
                return False
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{runtime_port}/health", timeout=0.75
                ) as response:
                    payload = json.loads(response.read(65_537).decode("utf-8"))
                return bool(
                    isinstance(payload, dict)
                    and payload.get("ok") is True
                    and payload.get("governance_ready") is True
                    and str(payload.get("tool_id") or "")
                    == expected_runtime_tool_id
                    and str(payload.get("workspace_instance_id") or "")
                    == self._workspace_instance_id()
                )
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
                return False

        ready = False
        # Local governed runtimes normally publish health in well under a
        # second. Poll more frequently so opening a tool feels immediate while
        # retaining a bounded five-second allowance for cold starts.
        for _ in range(300):
            if await asyncio.to_thread(source_runtime_ready):
                ready = True
                break
            await asyncio.sleep(0.1)
        if not ready:
            return {
                "ok": False,
                "error_code": "SOURCE_RUNTIME_NOT_READY",
                "message": "Governed source runtime did not become ready",
            }

        session_fingerprint = hashlib.sha256(
            "\0".join(
                (
                    expected_runtime_tool_id,
                    str(runtime_port),
                    str(runtime_environment.get("GPTBRIDGE_IPC_SESSION_TOKEN") or ""),
                    self._workspace_instance_id(),
                )
            ).encode("utf-8")
        ).hexdigest()
        existing = self._source_ui_processes.get(tool_id)
        if existing is not None and existing.returncode is None:
            if self._source_ui_runtime_sessions.get(tool_id) == session_fingerprint:
                return {
                    "ok": True,
                    "ui_pid": existing.pid,
                    "ui_mode": "governed-source-ui",
                }
            self._source_ui_processes.pop(tool_id, None)
            self._source_ui_runtime_sessions.pop(tool_id, None)
            await terminate_process_tree(existing)
            try:
                await asyncio.wait_for(existing.wait(), timeout=1)
            except asyncio.TimeoutError:
                await terminate_process_tree(existing)
        elif existing is None:
            # A UI inherited from an earlier main-system generation cannot
            # carry the newly issued runtime token or port. Remove the stale
            # process before creating the replacement session.
            orphaned_ui_ids = await asyncio.to_thread(
                self._running_source_ui_process_ids,
                tool_id,
            )
            if orphaned_ui_ids:
                await asyncio.to_thread(self._stop_running_source_ui, tool_id)
        renderer_entry = (
            self.project_root
            / "main-system"
            / "dist-ui"
            / "independent-tools"
            / tool_id
            / "renderer"
            / "index.html"
        ).resolve()
        host_entry = (
            self.project_root
            / "main-system"
            / "scripts"
            / "source-tool-ui-host"
            / "main.cjs"
        ).resolve()
        electron = (
            self.project_root
            / "main-system"
            / "node_modules"
            / "electron"
            / "dist"
            / "electron.exe"
        ).resolve()
        if not renderer_entry.is_file() or not host_entry.is_file() or not electron.is_file():
            return {
                "ok": False,
                "error_code": "SOURCE_UI_UNAVAILABLE",
                "message": "Governed source UI host or renderer is unavailable",
            }
        window = manifest.get("window") if isinstance(manifest.get("window"), dict) else {}
        environment = self._tool_environment(
            tool_id,
            tool_dir,
            manifest,
            governance_tool_id=expected_runtime_tool_id,
        )
        environment.update(
            {
                "GPTBRIDGE_SOURCE_UI_TOOL_ID": tool_id,
                "GPTBRIDGE_SOURCE_UI_WORKSPACE_ROOT": str(self.project_root.resolve()),
                "GPTBRIDGE_SOURCE_UI_TOOL_ROOT": str(tool_dir.resolve()),
                "GPTBRIDGE_SOURCE_UI_RENDERER_ENTRY": str(renderer_entry),
                "GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL": (
                    f"ws://127.0.0.1:{runtime_environment['GPTBRIDGE_IPC_PORT']}/"
                    f"?token={runtime_environment['GPTBRIDGE_IPC_SESSION_TOKEN']}"
                    f"&instance={self._workspace_instance_id()}"
                ),
                "GPTBRIDGE_SOURCE_UI_VERSION": str(manifest.get("version") or "1.0.0"),
                "GPTBRIDGE_SOURCE_UI_TITLE": str(
                    manifest.get("display_name") or tool_id
                ),
                "GPTBRIDGE_SOURCE_UI_WIDTH": str(window.get("width") or 1440),
                "GPTBRIDGE_SOURCE_UI_HEIGHT": str(window.get("height") or 920),
                "GPTBRIDGE_SOURCE_UI_MIN_WIDTH": str(window.get("minWidth") or 1120),
                "GPTBRIDGE_SOURCE_UI_MIN_HEIGHT": str(window.get("minHeight") or 760),
            }
        )
        process = await asyncio.create_subprocess_exec(
            str(electron),
            str(host_entry),
            f"--tool-id={tool_id}",
            cwd=str(self.project_root / "main-system"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            **_background_subprocess_kwargs(),
        )
        self._source_ui_processes[tool_id] = process
        self._source_ui_runtime_sessions[tool_id] = session_fingerprint

        async def forget_ui() -> None:
            await process.wait()
            if self._source_ui_processes.get(tool_id) is not process:
                return
            self._source_ui_processes.pop(tool_id, None)
            self._source_ui_runtime_sessions.pop(tool_id, None)
            if tool_id in self._force_closed_tool_ids:
                return
            try:
                await self.force_close_tool(
                    {
                        "tool_id": tool_id,
                        "request_id": f"window-close-{uuid.uuid4().hex}",
                        "reason": "independent-tool-window-closed",
                    }
                )
            except Exception:
                await self.update_status(tool_id, "stopped")

        asyncio.create_task(forget_ui())
        return {"ok": True, "ui_pid": process.pid, "ui_mode": "governed-source-ui"}

    # ------------------------------------------------------------------
    # Companion reconnect
    # ------------------------------------------------------------------

    async def _reconnect_companion_source_uis(self, runtime_owner_tool_id: str) -> None:
        """Reconnect open companion windows after their owner runtime changes session."""

        runtime_environment = self._source_runtime_environments.get(runtime_owner_tool_id)
        if runtime_environment is None:
            return
        for tool_dir in self._declared_companion_tool_directories():
            try:
                manifest = json.loads(
                    (tool_dir / "manifest.json").read_text(encoding="utf-8")
                )
                tool_id = str(manifest.get("id") or "").strip()
                owner = self._runtime_owner_tool_id(tool_id, manifest)
            except (OSError, ValueError, PermissionError, json.JSONDecodeError):
                continue
            if (
                not tool_id
                or owner != runtime_owner_tool_id
                or manifest.get("has_custom_ui") is not True
            ):
                continue
            current_ui = self._source_ui_processes.get(tool_id)
            if current_ui is None or current_ui.returncode is not None:
                continue
            await self._launch_source_ui(
                tool_id,
                tool_dir,
                manifest,
                runtime_environment,
                runtime_tool_id=runtime_owner_tool_id,
            )

    # ------------------------------------------------------------------
    # Existing-tool-window activation
    # ------------------------------------------------------------------

    async def _activate_existing_tool_window(
        self,
        *,
        tool_id: str,
        tool_dir: Path,
        executable_file: Path,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            activation_process = await asyncio.create_subprocess_exec(
                str(executable_file),
                cwd=str(tool_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._tool_environment(
                    tool_id,
                    tool_dir,
                    manifest,
                ),
                **_background_subprocess_kwargs(),
            )
        except Exception as exc:
            return {
                "activated": False,
                "activation_error": str(exc),
            }

        async def reap_activation_process() -> None:
            try:
                await activation_process.wait()
            except Exception:
                return

        asyncio.create_task(reap_activation_process())
        return {
            "activated": True,
            "activation_pid": activation_process.pid,
        }
