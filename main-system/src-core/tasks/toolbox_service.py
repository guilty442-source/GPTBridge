from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import stat as stat_module
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict
from managers.process_utils import terminate_process_tree
from governance_rule.execution.integrity.package_integrity import (
    load_package_metadata,
    verify_packaged_app,
)
from .tool_process_registry import (
    running_executable_process_ids,
    running_packaged_backend_process_ids,
    running_source_runtime_process_ids,
    running_source_ui_process_ids,
    stop_running_executable,
    stop_running_packaged_backend,
    stop_running_source_runtime,
    stop_running_source_ui,
)
from .central_repair import CentralRepairService
from .tool_path_resolver import ToolPathResolver
from core_system.permission_sovereign import PermissionSovereign

ToolEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]

MAX_TOOL_ARGUMENTS = 256
MAX_TOOL_ARGUMENT_BYTES = 256 * 1024
MAX_TOOL_REQUEST_ID_LENGTH = 128
MAX_TOOL_OUTPUT_CHARS = 2 * 1024 * 1024
MAX_TOOL_STREAM_LINE_BYTES = 4 * 1024 * 1024
_MANAGED_BACKEND_TOOL_ID_ENV = "GPTBRIDGE_MANAGED_BACKEND_TOOL_ID"
_MANAGED_BACKEND_WORKSPACE_ID_ENV = "GPTBRIDGE_MANAGED_BACKEND_WORKSPACE_INSTANCE_ID"
_MANAGED_BACKEND_VERSION_ENV = "GPTBRIDGE_MANAGED_BACKEND_VERSION"
_TOOL_GOVERNANCE_BOOTSTRAP_ENV = "GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP"
_REQUIRED_TOOL_VERSION = "1.0.0"
_REQUIRED_TOOL_DISPLAY_VERSION = "1.0"

_TOOL_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "GPTBRIDGE_POSTGRES_DSN",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USERPROFILE",
        "WINDIR",
        "XDG_STATE_HOME",
    }
)
_TOOL_ENVIRONMENT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
_FORBIDDEN_TOOL_ENVIRONMENT_MARKERS = frozenset(
    {"API_KEY", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN"}
)


def _is_declarable_tool_environment_key(value: Any) -> bool:
    key = str(value or "").strip().upper()
    return (
        _TOOL_ENVIRONMENT_KEY_PATTERN.fullmatch(key) is not None
        and key.startswith(("GPTBRIDGE_", "FILE_SORTER_"))
        and not any(marker in key for marker in _FORBIDDEN_TOOL_ENVIRONMENT_MARKERS)
    )


def _background_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    # Independent tools must survive a main-system crash or restart.
    # DETACHED_PROCESS removes the console parent/child coupling and
    # CREATE_NEW_PROCESS_GROUP isolates Ctrl+C/Ctrl+Break handling, so
    # terminating the main-system (or boot_core/main.py) does not cascade
    # through to independent tool processes.  CREATE_NO_WINDOW keeps them
    # headless.
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
    creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
    if not creationflags:
        return {}
    return {"creationflags": creationflags}


def _run_hidden_subprocess(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 4,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        **_background_subprocess_kwargs(),
    )


class ToolboxService:
    """Request broker for independently governed tools."""
    
    def __init__(
        self,
        project_root: Path,
        *,
        governance: Any = None,
        allowed_tool_ids: set[str] | frozenset[str] | None = None,
        permission_sovereign: Any = None,
    ):
        self.project_root = project_root
        self.tools_dir = self.project_root
        self.governance = governance
        self.permission_sovereign = (
            permission_sovereign
            if permission_sovereign is not None
            else PermissionSovereign(None, governance=governance)
        )
        self.allowed_tool_ids = (
            None
            if allowed_tool_ids is None
            else frozenset(str(item).strip() for item in allowed_tool_ids)
        )
        self._path_resolver = ToolPathResolver(
            self.project_root,
            self.allowed_tool_ids,
        )
        # Process ownership is request-scoped. The auxiliary tool index prevents
        # two jobs from mutating the same tool workspace at the same time.
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}
        self._request_tool_ids: dict[str, str] = {}
        self._request_kinds: dict[str, str] = {}
        self._active_request_by_tool: dict[str, str] = {}
        self._started_request_by_tool: dict[str, str] = {}
        self._cancelled_request_ids: set[str] = set()
        self._force_closed_tool_ids: set[str] = set()
        self._background_restart_attempts: dict[str, int] = {}
        self._background_restart_tasks: dict[str, asyncio.Task[Any]] = {}
        self._source_runtime_environments: dict[str, dict[str, str]] = {}
        self._source_ui_processes: dict[str, asyncio.subprocess.Process] = {}
        self._source_ui_runtime_sessions: dict[str, str] = {}
        self._process_state_lock = asyncio.Lock()
        self._central_repair: CentralRepairService | None = None
        # Manifest cache: tool_id -> (manifest_dict, tool_dir_path).
        # Avoids re-reading manifest.json 3+ times per tool start.
        self._manifest_cache: dict[str, tuple[Dict[str, Any], Path]] = {}
        # Reverse cache: tool_dir_name -> tool_id, for _tool_directory_for_id.
        self._tool_dir_index: dict[str, str] | None = None
        # Callback invoked on tool activity (set by Integration Sub-Sovereign
        # for idle management).  Signature: (tool_id: str) -> None.
        self._tool_activity_callback: Callable[[str], None] | None = None

    @property
    def central_repair(self) -> CentralRepairService:
        if self._central_repair is None:
            repair_data_root = self.project_root / "main-system" / "data" / "automatic-repair"
            repair_data_root.mkdir(parents=True, exist_ok=True)
            self._central_repair = CentralRepairService(
                self.project_root, repair_data_root
            )
        return self._central_repair

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

    def _runtime_owner_tool_id(
        self,
        tool_id: str,
        manifest: Dict[str, Any] | None = None,
    ) -> str:
        if manifest is None:
            try:
                tool_dir = self._tool_directory_for_id(tool_id)
                manifest = json.loads(
                    (tool_dir / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                return tool_id
        owner = str(manifest.get("runtime_owner_tool_id") or "").strip()
        if not owner or owner == tool_id:
            return tool_id
        declared_owner = str(
            manifest.get("host_tool_id")
            or manifest.get("shared_permission_owner")
            or ""
        ).strip()
        if owner != declared_owner:
            raise PermissionError("PERMISSION_DENIED")
        try:
            owner_dir = self._tool_directory_for_id(owner)
        except ValueError as error:
            raise PermissionError("PERMISSION_DENIED") from error
        if not (owner_dir / "manifest.json").is_file():
            raise PermissionError("PERMISSION_DENIED")
        return owner

    def _authorize_tool_lifecycle(self, tool_id: str, action: str) -> None:
        if self.governance is None:
            raise PermissionError("PERMISSION_DENIED")
        if action.casefold() in {"stop", "force-close", "force_close"}:
            try:
                manifest, _tool_dir = self._load_manifest_cached(tool_id)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise PermissionError("PERMISSION_DENIED") from error
            lifecycle = manifest.get("lifecycle")
            if isinstance(lifecycle, dict) and lifecycle.get("stoppable") is False:
                raise PermissionError("LIFECYCLE_LOCKED")
        authority_tool_id = self._runtime_owner_tool_id(tool_id)
        self.permission_sovereign.authorize_tool_lifecycle(authority_tool_id, action)

    @staticmethod
    def _governance_reason(check: Dict[str, Any]) -> str:
        reason = str(check.get("reason", "")).strip()
        if reason:
            return reason
        error_report = check.get("error_report")
        if isinstance(error_report, dict):
            root_cause = str(error_report.get("root_cause", "")).strip()
            if root_cause:
                return root_cause
        return "unknown governance rule"

    def _governance_blocked(self, check: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "message": f"GOVERNANCE BLOCKED: {self._governance_reason(check)}"}

    @staticmethod
    def _build_entry_content(tool_name: str) -> str:
        return (
            f"\"\"\"{tool_name} tool entry.\"\"\"\n\n"
            "def main() -> None:\n"
            f"    print(\"{tool_name} is ready on Windows 11\")\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        return ToolPathResolver.is_link_or_reparse_point(path)

    def _validated_tool_directory(self, tool_dir: Path) -> Path:
        return self._path_resolver.validated_tool_directory(tool_dir)

    def _validated_tool_path(
        self,
        tool_dir: Path,
        candidate: Path,
        *,
        label: str,
    ) -> Path:
        return self._path_resolver.validated_tool_path(
            tool_dir,
            candidate,
            label=label,
        )

    def _resolve_entry_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        return self._path_resolver.resolve_entry_file(manifest, tool_dir)

    def _resolve_working_directory(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        return self._path_resolver.resolve_working_directory(manifest, tool_dir)

    def _resolve_executable_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        return self._path_resolver.resolve_executable_file(manifest, tool_dir)

    def _resolve_python_executable(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        return self._path_resolver.resolve_python_executable(manifest, tool_dir)

    @staticmethod
    def _is_special_unpacked(manifest: Dict[str, Any]) -> bool:
        return ToolPathResolver.is_special_unpacked(manifest)

    @staticmethod
    def _has_governed_background_source(manifest: Dict[str, Any]) -> bool:
        return ToolPathResolver.has_governed_background_source(manifest)

    @staticmethod
    def _has_governed_source_runtime(manifest: Dict[str, Any]) -> bool:
        return ToolPathResolver.has_governed_source_runtime(manifest)

    @staticmethod
    def _is_dual_runtime(manifest: Dict[str, Any]) -> bool:
        return ToolPathResolver.is_dual_runtime(manifest)

    def _resolve_special_unpacked_entry(
        self,
        manifest: Dict[str, Any],
        tool_dir: Path,
    ) -> Path:
        return self._path_resolver.resolve_special_unpacked_entry(
            manifest,
            tool_dir,
        )

    @staticmethod
    def _allocate_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _source_runtime_environment(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
    ) -> dict[str, str]:
        child_env = self._tool_environment(
            tool_id,
            tool_dir,
            manifest,
            start_hidden=True,
        )
        child_env["GPTBRIDGE_IPC_PORT"] = str(self._allocate_loopback_port())
        child_env["GPTBRIDGE_IPC_SESSION_TOKEN"] = secrets.token_hex(32)
        child_env["GPTBRIDGE_SHUTDOWN_TOKEN"] = secrets.token_hex(32)
        return child_env

    def _workspace_instance_id(self) -> str:
        normalized = os.path.normcase(str(self.project_root.resolve())).replace(
            "\\", "/"
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _source_launch_requested(
        manifest: Dict[str, Any],
        *,
        background: bool,
        requested_mode: str,
        executable_exists: bool,
    ) -> bool:
        if not ToolboxService._has_governed_source_runtime(manifest):
            return False
        if requested_mode == "source":
            return True
        if requested_mode == "executable":
            return False
        if ToolboxService._is_special_unpacked(manifest):
            return True
        launch = manifest.get("launch")
        primary = str(launch.get("primary") or "") if isinstance(launch, dict) else ""
        if background:
            return True
        if ToolboxService._is_dual_runtime(manifest):
            return not executable_exists
        if ToolboxService._has_governed_background_source(manifest):
            return not executable_exists
        return primary != "executable" or not executable_exists

    @staticmethod
    def _source_fallback_allowed(
        manifest: Dict[str, Any],
        requested_mode: str,
    ) -> bool:
        if not ToolboxService._has_governed_source_runtime(manifest):
            return False
        if ToolboxService._is_dual_runtime(manifest):
            return requested_mode != "source"
        if requested_mode == "executable":
            return False
        launch = manifest.get("launch")
        return (
            isinstance(launch, dict)
            and str(launch.get("fallback") or "").strip().casefold()
            == "governed-source-ui"
        )

    @staticmethod
    def _executable_fallback_allowed(
        manifest: Dict[str, Any],
        requested_mode: str,
        executable_exists: bool,
    ) -> bool:
        if requested_mode == "executable" or not executable_exists:
            return False
        if ToolboxService._is_dual_runtime(manifest):
            return True
        launch = manifest.get("launch")
        return bool(
            isinstance(launch, dict)
            and str(launch.get("fallback") or "").strip().casefold()
            == "executable"
        )

    @staticmethod
    def _tool_version_failure(
        tool_id: str,
        request_id: str,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        version = str(manifest.get("version") or "").strip()
        display_version = str(manifest.get("display_version") or "").strip()
        errors: list[str] = []
        if version != _REQUIRED_TOOL_VERSION:
            errors.append(
                f"tool version must be {_REQUIRED_TOOL_VERSION}; "
                f"found {version or 'missing'}"
            )
        if display_version and display_version != _REQUIRED_TOOL_DISPLAY_VERSION:
            errors.append(
                "tool display version must be "
                f"{_REQUIRED_TOOL_DISPLAY_VERSION}; found {display_version}"
            )
        if not errors:
            return None
        return {
            "ok": False,
            "tool_id": tool_id,
            "request_id": request_id,
            "error_code": "TOOL_VERSION_MISMATCH",
            "message": "; ".join(errors),
        }

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

    async def _request_central_repair(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        failure: Dict[str, Any],
    ) -> dict[str, Any]:
        del tool_dir, manifest
        if tool_id in {"main-system"}:
            return {
                "triggered": False,
                "reason": "CENTRAL_REPAIR_OWNER_CANNOT_REPAIR_ITSELF",
            }
        failure_code = str(failure.get("error_code") or "TOOL_START_FAILED")[:128]
        try:
            from .package_rebuilder import ToolPackageRebuilder
            rebuilder = ToolPackageRebuilder(
                self.project_root,
                self.project_root / "main-system",
            )
            repair_result = await asyncio.to_thread(
                self.central_repair.repair_tool,
                tool_id,
                failure_code,
                package_rebuilder=rebuilder.rebuild,
            )
            extraction: dict[str, Any] | None = None
            extract_paths = repair_result.get("backup_extract_paths") or []
            if extract_paths and self.governance is not None:
                extraction = await self._request_backup_extraction(
                    tool_id, extract_paths
                )
            return {
                "triggered": True,
                "ok": repair_result.get("ok") is True,
                "authority": "main-system",
                "channel": "integrated-central-repair",
                "detail": repair_result,
                "backup_extraction": extraction,
            }
        except (OSError, ValueError, PermissionError) as error:
            return {
                "triggered": True,
                "ok": False,
                "error_code": "CENTRAL_AUTO_REPAIR_FAILED",
                "message": str(error),
            }

    async def _request_backup_extraction(
        self, target_tool_id: str, paths: list[str]
    ) -> dict[str, Any]:
        if not paths or not self.governance:
            return {"status": "skipped", "reason": "no-extraction-required"}
        started_here = False
        try:
            start_result = await self.start_tool(
                {
                    "tool_id": "global-cleaner",
                    "request_id": (
                        f"central-repair-extract-start-{time.time_ns()}"
                    ),
                    "background": True,
                    "runtime_mode": "source",
                    "_auto_repair_attempted": True,
                }
            )
            if start_result.get("ok") is not True:
                return {
                    "status": "service_start_failed",
                    "detail": start_result,
                }
            if "already running" not in str(
                start_result.get("message") or ""
            ).casefold():
                started_here = True
            extraction_args = [
                "--extract-managed-backup",
                "--backup-owner",
                target_tool_id,
            ]
            for path in paths:
                extraction_args.extend(("--backup-path", str(path)))
            extraction_args.append("--json")
            extraction_request_id = (
                f"repair-extract-{target_tool_id}-{time.time_ns()}"
            )
            queued = await self.request_tool_execution(
                {
                    "tool_id": "global-cleaner",
                    "request_id": extraction_request_id,
                    "_governed_command": "toolbox_request_tool_execution",
                    "args": extraction_args,
                }
            )
            if queued.get("ok") is not True:
                return {
                    "status": "queue_failed",
                    "detail": queued,
                }
            extraction = None
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                extraction = await asyncio.to_thread(
                    self.permission_sovereign.tool_execution_response,
                    "global-cleaner",
                    extraction_request_id,
                )
                if extraction and extraction.get("status") in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    break
                await asyncio.sleep(0.5)
            return extraction or {
                "status": "timed_out",
                "request_id": extraction_request_id,
            }
        except (OSError, ValueError, PermissionError) as error:
            return {"status": "failed", "message": str(error)}
        finally:
            if started_here:
                await self.force_close_tool(
                    {
                        "tool_id": "global-cleaner",
                        "request_id": (
                            f"central-repair-extract-stop-{time.time_ns()}"
                        ),
                    }
                )

    async def _retry_start_after_central_repair(
        self,
        payload: Dict[str, Any],
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        failure: Dict[str, Any],
    ) -> Dict[str, Any]:
        repair_result = await self._request_central_repair(
            tool_id,
            tool_dir,
            manifest,
            failure,
        )
        central_result = (
            repair_result.get("detail")
            if isinstance(repair_result.get("detail"), dict)
            else {}
        )
        package_repair = (
            central_result.get("package_repair")
            if isinstance(central_result, dict)
            else None
        )
        if not isinstance(package_repair, dict):
            package_repair = {
                "triggered": False,
                "owner": "main-system",
                "reason": "PACKAGE_REBUILD_NOT_REQUIRED",
            }
        if repair_result.get("ok") is not True:
            central_message = (
                central_result.get("message")
                if isinstance(central_result, dict)
                else None
            )
            return {
                **failure,
                "ok": False,
                "auto_repair": repair_result,
                "package_repair": package_repair,
                "message": str(
                    central_message
                    or "Central automatic repair could not restore tool stability"
                ),
            }
        retry_payload = dict(payload)
        retry_payload["_auto_repair_attempted"] = True
        retry_result = await self.start_tool(retry_payload)
        retry_result["auto_repair"] = repair_result
        retry_result["package_repair"] = package_repair
        return retry_result

    def _forget_missing_tool(self, tool_id: str) -> None:
        # Discovery is read-only. Missing tools are reflected directly from
        # the manifest directory and never persisted by the main system.
        return None

    @staticmethod
    def _missing_tool_result(tool_id: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": "舊應用程式資料已移除，請重新整理應用程式清單。",
            "removed": True,
        }

    def _tool_environment(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any] | None = None,
        *,
        start_hidden: bool = False,
        governance_tool_id: str | None = None,
    ) -> dict[str, str]:
        environment = manifest.get("environment") if isinstance(manifest, dict) else None
        declared_keys = environment.get("allow", []) if isinstance(environment, dict) else []
        if not isinstance(declared_keys, list):
            declared_keys = []
        tool_keys = {
            str(key).strip().upper()
            for key in declared_keys
            if _is_declarable_tool_environment_key(key)
        }
        allowed_keys = _TOOL_ENVIRONMENT_ALLOWLIST | tool_keys
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in allowed_keys
        }
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["PYTHONNOUSERSITE"] = "1"
        isolated_tool_root = tool_dir.resolve()
        isolated_data_root = isolated_tool_root / "runtime"
        cache_root = isolated_data_root / "cache"
        cache_owner_root = isolated_tool_root
        shared_cache_owner = str(
            (manifest.get("shared_cache_owner") or "")
            if isinstance(manifest, dict)
            else ""
        ).strip()
        if shared_cache_owner:
            permissions = (
                manifest.get("permissions") if isinstance(manifest, dict) else None
            )
            permission_owner = (
                str(permissions.get("business_permission_owner") or "").strip()
                if isinstance(permissions, dict)
                else ""
            )
            host_owner = str(manifest.get("host_tool_id") or "").strip()
            manifest_tool_id = str(manifest.get("id") or "").strip()
            if shared_cache_owner not in {
                permission_owner,
                host_owner,
                manifest_tool_id,
            }:
                raise ValueError("Shared cache owner is not the declared business owner")
            cache_owner_root = self._tool_directory_for_id(shared_cache_owner)
            cache_root = (
                cache_owner_root
                / "runtime"
                / "cache"
                / "companions"
                / tool_id
            )
            cache_root = self._validated_tool_path(
                cache_owner_root,
                cache_root,
                label="Shared tool cache storage",
            )
        cache_root.mkdir(parents=True, exist_ok=True)
        cleaner_root = self._validated_tool_directory(
            self.tools_dir / "global-cleaner"
        )
        temp_candidate = (
            cleaner_root / "runtime" / "temp" / "tools" / tool_id
        )
        isolated_temp_root = self._validated_tool_path(
            cleaner_root,
            temp_candidate,
            label="Tool temporary storage",
        )
        isolated_temp_root.mkdir(parents=True, exist_ok=True)
        isolated_temp_root = self._validated_tool_path(
            cleaner_root,
            isolated_temp_root,
            label="Tool temporary storage",
        )
        child_env["GPTBRIDGE_PROJECT_ROOT"] = str(isolated_tool_root)
        child_env["GPTBRIDGE_STANDALONE_TOOL_ID"] = tool_id
        child_env["GPTBRIDGE_TOOL_ID"] = tool_id
        child_env["GPTBRIDGE_TOOL_DIR"] = str(isolated_tool_root)
        child_env["GPTBRIDGE_TOOL_DATA_ROOT"] = str(isolated_data_root)
        child_env["GPTBRIDGE_TOOL_SETTINGS_ROOT"] = str(
            isolated_data_root / "settings"
        )
        child_env["GPTBRIDGE_TOOL_DATABASE_ROOT"] = str(
            isolated_data_root / "state"
        )
        child_env["GPTBRIDGE_TOOL_CACHE_ROOT"] = str(cache_root)
        child_env["GPTBRIDGE_TOOL_TEMP_ROOT"] = str(isolated_temp_root)
        child_env["TEMP"] = str(isolated_temp_root)
        child_env["TMP"] = str(isolated_temp_root)
        child_env["TMPDIR"] = str(isolated_temp_root)
        if self.governance is None:
            raise PermissionError("PERMISSION_DENIED")
        bootstrap_tool_id = governance_tool_id or tool_id
        child_env[_TOOL_GOVERNANCE_BOOTSTRAP_ENV] = (
            self.permission_sovereign.create_tool_governance_bootstrap(
                bootstrap_tool_id
            )
        )
        child_env["GPTBRIDGE_GOVERNANCE_PROJECT_ROOT"] = str(
            self.project_root.resolve()
        )
        if tool_id == "global-cleaner":
            child_env["GPTBRIDGE_GLOBAL_CLEANER_TARGET_ROOT"] = str(
                self.project_root.resolve()
            )
        child_env.pop("GPTBRIDGE_MANAGED_STORAGE_ROOT", None)
        child_env.pop("GPTBRIDGE_SYSTEM_RESCUE_STORAGE_AUTHORITY", None)
        if start_hidden:
            child_env["GPTBRIDGE_START_HIDDEN"] = "1"
        else:
            child_env.pop("GPTBRIDGE_START_HIDDEN", None)
        bindings = (
            environment.get("bindings", {})
            if isinstance(environment, dict)
            else {}
        )
        if isinstance(bindings, dict):
            for raw_key, source in bindings.items():
                key = str(raw_key).strip().upper()
                if not _is_declarable_tool_environment_key(key):
                    continue
                # A binding is controlled by the manifest, never by an
                # ambient parent-process value with the same name.
                child_env.pop(key, None)
                if source == "project_root":
                    # Tool manifests contain business configuration only and
                    # cannot grant project-wide authority to themselves.
                    continue
                elif source == "tool_root":
                    child_env[key] = str(tool_dir.resolve())
        managed_keys = (
            _MANAGED_BACKEND_TOOL_ID_ENV,
            _MANAGED_BACKEND_WORKSPACE_ID_ENV,
            _MANAGED_BACKEND_VERSION_ENV,
        )
        for key in managed_keys:
            child_env.pop(key, None)
        return child_env

    @staticmethod
    def _tool_arguments(payload: Dict[str, Any], tool_id: str = "") -> tuple[list[str] | None, Dict[str, Any] | None]:
        raw_args = payload.get("args", [])
        result_base = {"tool_id": tool_id} if tool_id else {}
        if not isinstance(raw_args, list):
            return None, {
                "ok": False,
                **result_base,
                "error_code": "INVALID_ARGUMENTS",
                "message": "args must be a list",
            }
        if len(raw_args) > MAX_TOOL_ARGUMENTS:
            return None, {
                "ok": False,
                **result_base,
                "error_code": "TOO_MANY_ARGUMENTS",
                "argument_count": len(raw_args),
                "max_arguments": MAX_TOOL_ARGUMENTS,
                "message": f"args exceeds the explicit limit of {MAX_TOOL_ARGUMENTS} items",
            }
        try:
            args = [str(item) for item in raw_args]
        except Exception as exc:
            return None, {
                "ok": False,
                **result_base,
                "error_code": "INVALID_ARGUMENTS",
                "message": f"args could not be converted to strings: {exc}",
            }
        argument_bytes = sum(len(arg.encode("utf-8")) for arg in args)
        if argument_bytes > MAX_TOOL_ARGUMENT_BYTES:
            return None, {
                "ok": False,
                **result_base,
                "error_code": "ARGUMENTS_TOO_LARGE",
                "argument_bytes": argument_bytes,
                "max_argument_bytes": MAX_TOOL_ARGUMENT_BYTES,
                "message": (
                    "encoded args exceeds the explicit limit of "
                    f"{MAX_TOOL_ARGUMENT_BYTES} bytes"
                ),
            }
        return args, None

    @staticmethod
    def _tool_request_id(payload: Dict[str, Any]) -> tuple[str | None, Dict[str, Any] | None]:
        raw_request_id = payload.get("request_id")
        request_id = str(raw_request_id).strip() if raw_request_id is not None else ""
        if not request_id:
            return uuid.uuid4().hex, None
        if len(request_id) > MAX_TOOL_REQUEST_ID_LENGTH or any(
            ord(character) < 32 for character in request_id
        ):
            return None, {
                "ok": False,
                "error_code": "INVALID_REQUEST_ID",
                "max_request_id_length": MAX_TOOL_REQUEST_ID_LENGTH,
                "message": (
                    "request_id must be a printable, non-empty string no longer than "
                    f"{MAX_TOOL_REQUEST_ID_LENGTH} characters"
                ),
            }
        return request_id, None

    async def _reserve_tool_process(
        self,
        *,
        request_id: str,
        tool_id: str,
        kind: str,
    ) -> Dict[str, Any] | None:
        async with self._process_state_lock:
            duplicate_tool_id = self._request_tool_ids.get(request_id)
            if duplicate_tool_id is not None:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "active_tool_id": duplicate_tool_id,
                    "error_code": "DUPLICATE_REQUEST_ID",
                    "message": "request_id is already active",
                }
            active_request_id = self._active_request_by_tool.get(tool_id)
            if active_request_id is not None:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "active_request_id": active_request_id,
                    "error_code": "TOOL_BUSY",
                    "message": "Another process is already active for this tool",
                }
            self._request_tool_ids[request_id] = tool_id
            self._request_kinds[request_id] = kind
            self._active_request_by_tool[tool_id] = request_id
        return None

    async def _register_tool_process(
        self,
        request_id: str,
        process: asyncio.subprocess.Process,
    ) -> bool:
        async with self._process_state_lock:
            if request_id not in self._request_tool_ids:
                return True
            self._running_processes[request_id] = process
            tool_id = self._request_tool_ids.get(request_id)
            if tool_id and self._request_kinds.get(request_id) == "started":
                self._started_request_by_tool[tool_id] = request_id
            return request_id in self._cancelled_request_ids

    async def _release_tool_process(self, request_id: str) -> bool:
        async with self._process_state_lock:
            tool_id = self._request_tool_ids.pop(request_id, None)
            self._request_kinds.pop(request_id, None)
            self._running_processes.pop(request_id, None)
            cancelled = request_id in self._cancelled_request_ids
            self._cancelled_request_ids.discard(request_id)
            if tool_id and self._active_request_by_tool.get(tool_id) == request_id:
                self._active_request_by_tool.pop(tool_id, None)
            if tool_id and self._started_request_by_tool.get(tool_id) == request_id:
                self._started_request_by_tool.pop(tool_id, None)
            return cancelled

    async def _release_started_tool_command_slot(self, request_id: str) -> None:
        """Let a standalone GUI issue commands while its EXE remains open."""

        async with self._process_state_lock:
            if self._request_kinds.get(request_id) != "started":
                return
            tool_id = self._request_tool_ids.get(request_id)
            if tool_id and self._active_request_by_tool.get(tool_id) == request_id:
                self._active_request_by_tool.pop(tool_id, None)

    async def _active_tool_process(
        self,
        tool_id: str,
    ) -> tuple[str | None, asyncio.subprocess.Process | None, str | None]:
        async with self._process_state_lock:
            request_id = self._active_request_by_tool.get(tool_id)
            if request_id is None:
                return None, None, None
            return (
                request_id,
                self._running_processes.get(request_id),
                self._request_kinds.get(request_id),
            )

    async def _started_tool_process(
        self,
        tool_id: str,
    ) -> tuple[str | None, asyncio.subprocess.Process | None]:
        async with self._process_state_lock:
            request_id = self._started_request_by_tool.get(tool_id)
            if request_id is None:
                return None, None
            return request_id, self._running_processes.get(request_id)

    def _manifest_to_record(self, tool_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
        manifest_path = tool_dir / "manifest.json"
        entry_file = self._resolve_entry_file(manifest, tool_dir)
        executable_file = self._resolve_executable_file(manifest, tool_dir)
        source_runtime = self._has_governed_source_runtime(manifest)
        source_runtime_entry = (
            self._resolve_special_unpacked_entry(manifest, tool_dir)
            if source_runtime
            else None
        )
        record = dict(manifest)
        locale_path = tool_dir / "locales" / "zh-TW.json"
        try:
            locale = json.loads(locale_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            locale = {}
        if isinstance(locale, dict):
            name_key = str(manifest.get("name_key", "")).strip()
            description_key = str(manifest.get("description_key", "")).strip()
            localized_name = locale.get(name_key) if name_key else None
            localized_description = locale.get(description_key) if description_key else None
            if isinstance(localized_name, str) and localized_name.strip():
                record["name"] = localized_name.strip()
            if isinstance(localized_description, str) and localized_description.strip():
                record["description"] = localized_description.strip()
        record["folder_path"] = str(tool_dir)
        record["manifest_path"] = str(manifest_path)
        record["code_path"] = str(entry_file)
        record["standalone"] = True
        permissions = manifest.get("permissions")
        permissions = permissions if isinstance(permissions, dict) else {}
        record["data_boundary"] = {
            "standalone": True,
            "code_scope": str(permissions.get("code_scope") or "").strip(),
            "database_scope": str(permissions.get("database_scope") or "").strip(),
        }
        record["executable_path"] = str(executable_file)
        record["executable_exists"] = executable_file.exists()
        launch = manifest.get("launch")
        primary_launch = (
            str(launch.get("primary") or "").strip().casefold()
            if isinstance(launch, dict)
            else ""
        )
        if self._is_dual_runtime(manifest):
            record["runtime_mode"] = "dual-runtime"
            record["automatic_runtime_mode"] = (
                "executable" if executable_file.exists() else "governed-source"
            )
        else:
            record["runtime_mode"] = (
                "executable"
                if executable_file.exists() and primary_launch == "executable"
                else "governed-source"
                if source_runtime
                else "executable"
            )
        record["runtime_available"] = bool(
            source_runtime_entry is not None or executable_file.exists()
        )
        if source_runtime_entry is not None:
            record["source_runtime_entry"] = str(source_runtime_entry)
        # Folder size is populated by the trusted Electron main process. Do
        # not send a false zero because the renderer treats numeric values as
        # authoritative and would skip the local inventory fallback.
        record["project_size_bytes"] = None
        return record

    def _declared_companion_tool_directories(self) -> list[Path]:
        companions: list[Path] = []
        if not self.tools_dir.exists():
            return companions
        for host_dir in sorted(self.tools_dir.iterdir(), key=lambda item: item.name.lower()):
            host_manifest_path = host_dir / "manifest.json"
            if not host_dir.is_dir() or not host_manifest_path.is_file():
                continue
            try:
                host_manifest = json.loads(host_manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            declarations = host_manifest.get("companion_tools")
            if not isinstance(declarations, list):
                continue
            for declaration in declarations:
                if not isinstance(declaration, dict):
                    continue
                relative_path = str(declaration.get("path") or "").strip()
                declared_id = str(declaration.get("id") or "").strip()
                if not relative_path or not declared_id:
                    continue
                candidate = host_dir / relative_path
                try:
                    candidate = self._validated_tool_directory(candidate)
                    manifest = json.loads(
                        (candidate / "manifest.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if manifest.get("id") != declared_id:
                    continue
                companions.append(candidate)
        return companions

    def _load_manifest_cached(self, tool_id: str) -> tuple[Dict[str, Any], Path]:
        """Load and cache a tool's manifest, returning (manifest, tool_dir).

        Avoids redundant manifest.json reads during startup — a single
        tool start previously triggered 3+ manifest reads.
        """
        cached = self._manifest_cache.get(tool_id)
        if cached is not None:
            return cached
        tool_dir = self._tool_directory_for_id(tool_id)
        manifest = json.loads(
            (tool_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self._manifest_cache[tool_id] = (manifest, tool_dir)
        return manifest, tool_dir

    def _build_tool_dir_index(self) -> dict[str, str]:
        """Build a one-time index of tool_dir_name -> tool_id.

        Replaces the O(n) scan in _tool_directory_for_id with an O(1)
        lookup after the first call.
        """
        if self._tool_dir_index is not None:
            return self._tool_dir_index
        index: dict[str, str] = {}
        if self.tools_dir.exists():
            for candidate in self.tools_dir.iterdir():
                manifest_path = candidate / "manifest.json"
                if not candidate.is_dir() or not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                tid = str(manifest.get("id") or "").strip()
                if tid:
                    index[candidate.name] = tid
                    self._manifest_cache[tid] = (manifest, candidate)
        self._tool_dir_index = index
        return index

    def _tool_directory_for_id(self, tool_id: str) -> Path:
        direct = self.tools_dir / tool_id
        if (direct / "manifest.json").is_file():
            return self._validated_tool_directory(direct)
        # Use the cached index instead of scanning every directory each time.
        index = self._build_tool_dir_index()
        for dir_name, tid in index.items():
            if tid == tool_id:
                return self._validated_tool_directory(self.tools_dir / dir_name)
        for companion in self._declared_companion_tool_directories():
            try:
                manifest = json.loads(
                    (companion / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("id") == tool_id:
                self._manifest_cache[tool_id] = (manifest, companion)
                return companion
        raise ValueError(f"Tool directory is unavailable: {tool_id}")

    def _load_manifest_records(self) -> list[Dict[str, Any]]:
        records: list[Dict[str, Any]] = []
        if not self.tools_dir.exists():
            return records

        for tool_dir in sorted(self.tools_dir.iterdir(), key=lambda item: item.name.lower()):
            manifest_path = tool_dir / "manifest.json"
            if not tool_dir.is_dir() or not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                records.append(self._manifest_to_record(tool_dir, manifest))
            except (OSError, ValueError):
                continue
        known_ids = {str(record.get("id") or "") for record in records}
        for tool_dir in self._declared_companion_tool_directories():
            try:
                manifest = json.loads((tool_dir / "manifest.json").read_text(encoding="utf-8"))
                tool_id = str(manifest.get("id") or "")
                if tool_id and tool_id not in known_ids:
                    records.append(self._manifest_to_record(tool_dir, manifest))
                    known_ids.add(tool_id)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return records

    async def list_tools(self) -> Dict[str, Any]:
        tools = self._load_manifest_records()
        # Infrastructure / authority modules are not user-facing tools and
        # must not appear as toolbox cards on the main screen. They remain
        # governed and supervised but are hidden from the toolbox UI:
        #   - global-cleaner  : governed backup/cleanup infrastructure
        hidden_infrastructure_ids = {
            "global-cleaner",
            "governance_rule",
        }
        for tool in tools:
            tool_id = str(tool.get("id", "")).strip()
            if tool_id in hidden_infrastructure_ids:
                tool["hidden_from_toolbox"] = True
            if tool_id in {"shared-layer", "xingcheng"}:
                tool["permission_denied"] = False
                tool["lifecycle_locked"] = True
                tool["resident_service"] = True
                tool["status"] = "running"
                continue
            # Classify resident vs non-resident from manifest lifecycle.
            lifecycle = tool.get("lifecycle") or {}
            tool["resident_service"] = lifecycle.get("stoppable") is False
            try:
                authority_tool_id = self._runtime_owner_tool_id(tool_id, tool)
                authorized = bool(
                    tool_id
                    and self.permission_sovereign.can_start_tool(authority_tool_id)
                )
            except PermissionError:
                authorized = False
            tool["permission_denied"] = not authorized
            executable_path = str(tool.get("executable_path", "")).strip()
            source_runtime_entry = str(tool.get("source_runtime_entry", "")).strip()
            running = bool(
                authorized
                and (
                    (
                        source_runtime_entry
                        and self._running_source_runtime_process_ids(
                            Path(source_runtime_entry)
                        )
                    )
                    or (
                        executable_path
                        and self._running_executable_process_ids(Path(executable_path))
                    )
                    or bool(self._running_source_ui_process_ids(tool_id))
                )
            )
            tool["status"] = "running" if running else "stopped"
        return {"ok": True, "tools": tools}

    async def update_status(self, tool_id: str, status: str) -> Dict[str, Any]:
        try:
            manifest_path = self._tool_directory_for_id(tool_id) / "manifest.json"
        except ValueError:
            self._forget_missing_tool(tool_id)
            return self._missing_tool_result(tool_id)
        # Runtime status is process-derived and intentionally not stored in a
        # shared database. Each independent tool owns its own business state.
        return {"ok": True, "tool_id": tool_id, "status": status}

    _running_executable_process_ids = staticmethod(running_executable_process_ids)
    _running_source_runtime_process_ids = staticmethod(
        running_source_runtime_process_ids
    )
    _running_packaged_backend_process_ids = staticmethod(
        running_packaged_backend_process_ids
    )
    _running_source_ui_process_ids = staticmethod(running_source_ui_process_ids)
    _stop_running_source_ui = staticmethod(stop_running_source_ui)
    _stop_running_executable = staticmethod(stop_running_executable)
    _stop_running_source_runtime = staticmethod(stop_running_source_runtime)
    _stop_running_packaged_backend = staticmethod(stop_running_packaged_backend)

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

    async def start_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        background = payload.get("background") is True
        managed_restart = payload.get("_managed_restart") is True
        repair_attempted = payload.get("_auto_repair_attempted") is True
        fallback_attempted = payload.get("_source_fallback_attempted") is True
        executable_fallback_attempted = (
            payload.get("_executable_fallback_attempted") is True
        )
        requested_mode = str(payload.get("runtime_mode") or "").strip().casefold()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}
        try:
            self._authorize_tool_lifecycle(tool_id, "start")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        if not managed_restart:
            self._force_closed_tool_ids.discard(tool_id)
            self._background_restart_attempts.pop(tool_id, None)

        request_id, request_error = self._tool_request_id(payload)
        if request_error is not None or request_id is None:
            return {"tool_id": tool_id, **(request_error or {})}
        args, argument_error = self._tool_arguments(payload, tool_id)
        if argument_error is not None or args is None:
            if argument_error is not None:
                argument_error["request_id"] = request_id
            return argument_error or {"ok": False, "tool_id": tool_id, "request_id": request_id}

        try:
            tool_dir = self._tool_directory_for_id(tool_id)
        except ValueError as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "INVALID_TOOL_PATH",
                "message": str(error),
            }
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            self._forget_missing_tool(tool_id)
            return {**self._missing_tool_result(tool_id), "request_id": request_id}

        try:
            manifest, _cached_dir = self._load_manifest_cached(tool_id)
        except Exception as exc:
            return {"ok": False, "tool_id": tool_id, "message": f"Invalid tool manifest: {exc}"}

        if manifest.get("enabled", True) is False:
            return {"ok": False, "tool_id": tool_id, "message": "Tool is disabled"}

        runtime_owner_tool_id = self._runtime_owner_tool_id(tool_id, manifest)
        if runtime_owner_tool_id != tool_id:
            owner_result = await self.start_tool(
                {
                    "tool_id": runtime_owner_tool_id,
                    "request_id": f"{request_id}:owner",
                    "runtime_mode": "source",
                    "background": True,
                }
            )
            if owner_result.get("ok") is not True:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "error_code": "RUNTIME_OWNER_NOT_READY",
                    "message": "Shared owner runtime could not be started",
                    "owner_result": owner_result,
                }
            runtime_environment = self._source_runtime_environments.get(
                runtime_owner_tool_id
            )
            if runtime_environment is None:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "error_code": "RUNTIME_OWNER_NOT_READY",
                    "message": "Shared owner runtime session is unavailable",
                }
            ui_result: dict[str, Any] = {}
            if not background and manifest.get("has_custom_ui") is True:
                ui_result = await self._launch_source_ui(
                    tool_id,
                    tool_dir,
                    manifest,
                    runtime_environment,
                    runtime_tool_id=runtime_owner_tool_id,
                )
                if ui_result.get("ok") is not True:
                    return {
                        "tool_id": tool_id,
                        "request_id": request_id,
                        **ui_result,
                    }
            await self.update_status(tool_id, "running")
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "runtime_owner_tool_id": runtime_owner_tool_id,
                "runtime_mode": "shared-owner-runtime",
                "background": background,
                "message": "Companion interface connected to its shared owner runtime",
                **ui_result,
            }

        version_failure = self._tool_version_failure(tool_id, request_id, manifest)
        if version_failure is not None:
            await self.update_status(tool_id, "error")
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    version_failure,
                )
            return version_failure

        source_runtime = self._has_governed_source_runtime(manifest)
        source_entry: Path | None = None
        python_executable: Path | None = None
        try:
            executable_file = self._resolve_executable_file(manifest, tool_dir)
            if source_runtime:
                source_entry = self._resolve_special_unpacked_entry(
                    manifest,
                    tool_dir,
                )
                python_executable = self._resolve_python_executable(
                    manifest,
                    tool_dir,
                )
        except ValueError as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": (
                    "INVALID_SOURCE_RUNTIME"
                    if source_runtime
                    else "INVALID_EXECUTABLE_PATH"
                ),
                "message": str(error),
            }
        launch = manifest.get("launch")
        primary_launch = (
            str(launch.get("primary") or "").strip().casefold()
            if isinstance(launch, dict)
            else ""
        )
        use_source_runtime = self._source_launch_requested(
            manifest,
            background=background,
            requested_mode=requested_mode,
            executable_exists=executable_file.exists(),
        )
        executable_preferred = (
            not use_source_runtime
            and not background
            and requested_mode != "source"
            and (requested_mode == "executable" or primary_launch == "executable")
        )
        if executable_preferred and not executable_file.exists() and not repair_attempted:
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "EXECUTABLE_MISSING",
                "message": "Standalone EXE is missing",
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            return await self._retry_start_after_central_repair(
                payload,
                tool_id,
                tool_dir,
                manifest,
                failure_result,
            )
        runtime_path = source_entry if use_source_runtime else executable_file
        runtime_mode = "governed-source" if use_source_runtime else "executable"
        if not use_source_runtime and not executable_file.exists():
            await self.update_status(tool_id, "stopped")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "EXECUTABLE_MISSING",
                "message": f"Standalone EXE not found. Run npm run package:tool -- {tool_id}",
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            return failure_result

        package_check = (
            {"ok": True}
            if use_source_runtime
            else verify_packaged_app(
                executable_file.parent / "resources" / "app",
            )
        )
        if not package_check.get("ok"):
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "PACKAGE_UNVERIFIED",
                "package_error_code": str(package_check.get("error_code") or ""),
                "message": (
                    f"{package_check.get('message', 'Package verification failed')}. "
                    f"Run npm run package:tool -- {tool_id}"
                ),
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            if self._source_fallback_allowed(manifest, requested_mode):
                use_source_runtime = True
                runtime_path = source_entry
                runtime_mode = "governed-source"
                package_check = {"ok": True}
            else:
                return failure_result
        package_metadata = (
            {}
            if use_source_runtime
            else load_package_metadata(
                executable_file.parent / "resources" / "app"
            )
        )
        source_version = str(manifest.get("version") or "").strip()
        packaged_version = str(
            package_metadata.get("tool_version") or source_version
        ).strip()
        if (
            not use_source_runtime
            and (not source_version or packaged_version != source_version)
        ):
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "STALE_TOOL_PACKAGE",
                "message": (
                    "This tool's manifest version changed after its EXE was packaged. "
                    f"Run npm run package:tool -- {tool_id}"
                ),
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            if self._source_fallback_allowed(manifest, requested_mode):
                use_source_runtime = True
                runtime_path = source_entry
                runtime_mode = "governed-source"
                package_metadata = {}
            else:
                return failure_result
        try:
            contract = json.loads(
                (
                    self.project_root
                    / "main-system"
                    / "config"
                    / "tool-runtime-contract.json"
                ).read_text(encoding="utf-8")
            )
            current_contract = int(contract["contract_version"])
            minimum_contract = int(
                contract["minimum_supported_contract_version"]
            )
            packaged_contract = int(
                package_metadata.get("runtime_contract_version") or 1
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            current_contract = 1
            minimum_contract = 1
            packaged_contract = int(
                package_metadata.get("runtime_contract_version") or 1
            )
        if (
            not use_source_runtime
            and not minimum_contract <= packaged_contract <= current_contract
        ):
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "INCOMPATIBLE_TOOL_RUNTIME",
                "message": (
                    "The packaged tool runtime contract is incompatible with "
                    "this GPTBridge version."
                ),
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            if self._source_fallback_allowed(manifest, requested_mode):
                use_source_runtime = True
                runtime_path = source_entry
                runtime_mode = "governed-source"
            else:
                return failure_result

        tracked_request_id, tracked_process = await self._started_tool_process(tool_id)
        if (
            tracked_request_id is not None
            and tracked_process is not None
            and tracked_process.returncode is None
        ):
            if background or use_source_runtime:
                ui_result: dict[str, Any] = {}
                if (
                    use_source_runtime
                    and not background
                    and manifest.get("has_custom_ui") is True
                ):
                    runtime_environment = self._source_runtime_environments.get(tool_id)
                    if runtime_environment is not None:
                        ui_result = await self._launch_source_ui(
                            tool_id,
                            tool_dir,
                            manifest,
                            runtime_environment,
                        )
                status_result = await self.update_status(tool_id, "running")
                if not status_result.get("ok"):
                    return status_result
                return {
                    "ok": True,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "pid": tracked_process.pid,
                    "runtime_path": str(runtime_path),
                    "runtime_mode": runtime_mode,
                    "background": True,
                    "message": "Tool runtime is already running in background",
                    **ui_result,
                }
            activation = await self._activate_existing_tool_window(
                tool_id=tool_id,
                tool_dir=tool_dir,
                executable_file=executable_file,
                manifest=manifest,
            )
            status_result = await self.update_status(tool_id, "running")
            if not status_result.get("ok"):
                return status_result
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "pid": tracked_process.pid,
                "executable_path": str(executable_file),
                "message": "Tool executable is already running; activation requested",
                **activation,
            }

        reservation_error = await self._reserve_tool_process(
            request_id=request_id,
            tool_id=tool_id,
            kind="started",
        )
        if reservation_error is not None:
            return reservation_error

        running_process_ids = (
            self._running_source_runtime_process_ids(source_entry)
            if use_source_runtime and source_entry is not None
            else self._running_executable_process_ids(executable_file)
        )
        if running_process_ids and use_source_runtime and source_entry is not None:
            # A source runtime left behind by an earlier main-system generation
            # cannot be supervised or restarted by this ToolboxService instance.
            # Replace it with a freshly governed, owned process.
            await asyncio.to_thread(self._stop_running_source_runtime, source_entry)
            for _ in range(20):
                if not self._running_source_runtime_process_ids(source_entry):
                    break
                await asyncio.sleep(0.1)
            running_process_ids = self._running_source_runtime_process_ids(source_entry)
        if running_process_ids:
            await self._release_tool_process(request_id)
            if background or use_source_runtime:
                ui_result: dict[str, Any] = {}
                if (
                    use_source_runtime
                    and not background
                    and manifest.get("has_custom_ui") is True
                ):
                    runtime_environment = self._source_runtime_environments.get(tool_id)
                    if runtime_environment is not None:
                        ui_result = await self._launch_source_ui(
                            tool_id,
                            tool_dir,
                            manifest,
                            runtime_environment,
                        )
                status_result = await self.update_status(tool_id, "running")
                if not status_result.get("ok"):
                    return status_result
                return {
                    "ok": True,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "pid": running_process_ids[0],
                    "runtime_path": str(runtime_path),
                    "runtime_mode": runtime_mode,
                    "background": True,
                    "message": "Tool runtime is already running in background",
                    **ui_result,
                }
            activation = await self._activate_existing_tool_window(
                tool_id=tool_id,
                tool_dir=tool_dir,
                executable_file=executable_file,
                manifest=manifest,
            )
            status_result = await self.update_status(tool_id, "running")
            if not status_result.get("ok"):
                return status_result
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "pid": running_process_ids[0],
                "executable_path": str(executable_file),
                "message": "Tool executable is already running; activation requested",
                **activation,
            }

        try:
            if use_source_runtime and source_entry is not None and python_executable is not None:
                source_environment = self._source_runtime_environment(
                    tool_id,
                    tool_dir,
                    manifest,
                )
                process = await asyncio.create_subprocess_exec(
                    str(python_executable),
                    "-B",
                    "-s",
                    "-E",
                    "-X",
                    "utf8",
                    str(source_entry),
                    *args,
                    cwd=str(tool_dir),
                    stdout=subprocess.DEVNULL,
                    # Preserve governed runtime failures in the main backend
                    # diagnostic stream so automatic repair can classify an
                    # immediate-exit failure instead of reporting a false start.
                    stderr=None,
                    env=source_environment,
                    **_background_subprocess_kwargs(),
                )
                self._source_runtime_environments[tool_id] = source_environment
            else:
                process = await asyncio.create_subprocess_exec(
                    str(executable_file),
                    *args,
                    cwd=str(tool_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self._tool_environment(
                        tool_id,
                        tool_dir,
                        manifest,
                        start_hidden=background,
                    ),
                    **_background_subprocess_kwargs(),
                )
        except Exception as exc:
            await self._release_tool_process(request_id)
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "PROCESS_START_FAILED",
                "message": str(exc),
            }
            if (
                use_source_runtime
                and not executable_fallback_attempted
                and self._executable_fallback_allowed(
                    manifest,
                    requested_mode,
                    executable_file.exists(),
                )
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "executable"
                fallback_payload["_executable_fallback_attempted"] = True
                fallback_payload["_source_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if (
                not use_source_runtime
                and not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            return failure_result

        if use_source_runtime:
            # Resident services must survive their initialization window.
            # Treat an immediate exit as a failed start so System Rescue can
            # diagnose and retry it instead of publishing a false "running" state.
            for _ in range(3):
                if process.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            if process.returncode is not None:
                await self._release_tool_process(request_id)
                await self.update_status(tool_id, "error")
                failure_result = {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "error_code": "SOURCE_RUNTIME_EXITED",
                    "message": "Governed source runtime exited during startup",
                    "exit_code": process.returncode,
                }
                if not repair_attempted:
                    return await self._retry_start_after_central_repair(
                        payload,
                        tool_id,
                        tool_dir,
                        manifest,
                        failure_result,
                    )
                return failure_result

        cancel_pending = await self._register_tool_process(request_id, process)
        asyncio.create_task(
            self._watch_started_tool(
                request_id,
                tool_id,
                runtime_path,
                use_source_runtime,
                process,
                close_program_on_exit=(
                    manifest.get("has_custom_ui") is True and not background
                ),
            )
        )
        if self._background_restart_policy(tool_id) is not None:
            asyncio.create_task(
                self._reset_restart_attempts_after_stability(tool_id, process)
            )
        await self._release_started_tool_command_slot(request_id)
        if cancel_pending and process.returncode is None:
            await terminate_process_tree(process)
        status_result = await self.update_status(tool_id, "running")
        ui_result: dict[str, Any] = {}
        if (
            use_source_runtime
            and not background
            and manifest.get("has_custom_ui") is True
        ):
            runtime_environment = self._source_runtime_environments.get(tool_id)
            if runtime_environment is not None:
                ui_result = await self._launch_source_ui(
                    tool_id,
                    tool_dir,
                    manifest,
                    runtime_environment,
                )
            else:
                ui_result = {
                    "ok": False,
                    "error_code": "SOURCE_UI_UNAVAILABLE",
                    "message": "Governed source UI session is unavailable",
                }
        if ui_result and not ui_result.get("ok") and not repair_attempted:
            failure_result = {
                "tool_id": tool_id,
                "request_id": request_id,
                **ui_result,
            }
            await self.force_close_tool(
                {
                    "tool_id": tool_id,
                    "request_id": f"central-repair-close-{uuid.uuid4().hex}",
                }
            )
            if (
                not executable_fallback_attempted
                and self._executable_fallback_allowed(
                    manifest,
                    requested_mode,
                    executable_file.exists(),
                )
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "executable"
                fallback_payload["_executable_fallback_attempted"] = True
                fallback_payload["_source_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            return await self._retry_start_after_central_repair(
                payload,
                tool_id,
                tool_dir,
                manifest,
                failure_result,
            )
        if not status_result.get("ok"):
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "pid": process.pid,
                "runtime_path": str(runtime_path),
                "runtime_mode": runtime_mode,
                "status_warning": str(status_result.get("message", "status update failed")),
                "message": "Tool runtime started",
                **ui_result,
            }
        return {
            "ok": True,
            "tool_id": tool_id,
            "request_id": request_id,
            "pid": process.pid,
            "runtime_path": str(runtime_path),
            "runtime_mode": runtime_mode,
            "background": background,
            "message": "Tool runtime started",
            **ui_result,
        }

    async def _watch_started_tool(
        self,
        request_id: str,
        tool_id: str,
        runtime_path: Path,
        source_runtime: bool,
        process: asyncio.subprocess.Process,
        close_program_on_exit: bool = False,
    ) -> None:
        try:
            await process.wait()
        finally:
            cancelled = await self._release_tool_process(request_id)
            if (
                close_program_on_exit
                and not cancelled
                and tool_id not in self._force_closed_tool_ids
            ):
                try:
                    await self.force_close_tool(
                        {
                            "tool_id": tool_id,
                            "request_id": f"window-close-{uuid.uuid4().hex}",
                            "reason": "independent-tool-window-closed",
                        }
                    )
                finally:
                    await self.update_status(tool_id, "stopped")
                return
            still_running = bool(
                self._running_source_runtime_process_ids(runtime_path)
                if source_runtime
                else self._running_executable_process_ids(runtime_path)
            )
            await self.update_status(
                tool_id,
                "running" if still_running and not cancelled else "stopped",
            )
            if not cancelled and not still_running:
                self._schedule_background_restart(tool_id)

    async def stop_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility alias for clients that still send the old stop command."""

        return await self.force_close_tool(payload)

    async def force_close_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Force-close the complete tool process tree and verify no process remains."""

        tool_id = str(payload.get("tool_id", "")).strip()
        command_request_id = str(payload.get("request_id") or "").strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}
        try:
            self._authorize_tool_lifecycle(tool_id, "stop")
        except PermissionError as error:
            error_code = str(error) or "PERMISSION_DENIED"
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": command_request_id,
                "error_code": error_code,
                "message": error_code,
            }

        self._force_closed_tool_ids.add(tool_id)
        restart_task = self._background_restart_tasks.pop(tool_id, None)
        if restart_task is not None and not restart_task.done():
            restart_task.cancel()

        tracked_request_id, process, _kind = await self._active_tool_process(tool_id)
        if tracked_request_id is not None:
            async with self._process_state_lock:
                self._cancelled_request_ids.add(tracked_request_id)

        started_request_id, started_process = await self._started_tool_process(tool_id)
        if started_request_id is not None:
            async with self._process_state_lock:
                self._cancelled_request_ids.add(started_request_id)

        tracked_processes = {
            id(candidate): candidate
            for candidate in (process, started_process)
            if candidate is not None and candidate.returncode is None
        }
        force_closed_process_ids: set[int] = set()
        source_ui_process = self._source_ui_processes.pop(tool_id, None)
        self._source_ui_runtime_sessions.pop(tool_id, None)
        self._source_runtime_environments.pop(tool_id, None)
        if source_ui_process is not None and source_ui_process.returncode is None:
            if source_ui_process.pid:
                force_closed_process_ids.add(int(source_ui_process.pid))
            await terminate_process_tree(source_ui_process)
        for candidate in tracked_processes.values():
            if candidate.pid:
                force_closed_process_ids.add(int(candidate.pid))
            await terminate_process_tree(candidate)
            try:
                await asyncio.wait_for(candidate.wait(), timeout=1)
            except asyncio.TimeoutError:
                await terminate_process_tree(candidate)

        try:
            tool_dir = self._tool_directory_for_id(tool_id)
        except ValueError as error:
            return failure(str(error), "INVALID_TOOL_PATH")
        manifest_path = tool_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                executable_file = self._resolve_executable_file(manifest, tool_dir)
                if self._has_governed_source_runtime(manifest):
                    source_entry = self._resolve_special_unpacked_entry(
                        manifest,
                        tool_dir,
                    )
                    for _attempt in range(2):
                        force_closed_process_ids.update(
                            self._stop_running_source_runtime(source_entry)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_executable(executable_file)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_packaged_backend(tool_dir)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_source_ui(tool_id)
                        )
                    remaining_process_ids = sorted(
                        set(self._running_source_runtime_process_ids(source_entry))
                        | set(self._running_executable_process_ids(executable_file))
                        | set(self._running_packaged_backend_process_ids(tool_dir))
                        | set(self._running_source_ui_process_ids(tool_id))
                    )
                else:
                    for _attempt in range(2):
                        force_closed_process_ids.update(
                            self._stop_running_executable(executable_file)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_packaged_backend(tool_dir)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_source_ui(tool_id)
                        )
                    remaining_process_ids = sorted(
                        set(self._running_executable_process_ids(executable_file))
                        | set(self._running_packaged_backend_process_ids(tool_dir))
                        | set(self._running_source_ui_process_ids(tool_id))
                    )
            except Exception as error:
                await self.update_status(tool_id, "running")
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": command_request_id,
                    "error_code": "FORCE_CLOSE_FAILED",
                    "remaining_process_ids": [],
                    "message": (
                        "Forced close could not verify the tool process state: "
                        f"{type(error).__name__}"
                    ),
                }
        else:
            self._forget_missing_tool(tool_id)
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": command_request_id,
                "message": "舊應用程式資料已移除。",
                "force_closed": True,
                "removed": True,
            }

        if remaining_process_ids:
            await self.update_status(tool_id, "running")
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": command_request_id,
                "error_code": "FORCE_CLOSE_FAILED",
                "remaining_process_ids": remaining_process_ids,
                "message": "Tool process remains after forced close",
            }

        status_result = await self.update_status(tool_id, "stopped")
        if not status_result.get("ok"):
            return status_result
        return {
            "ok": True,
            "tool_id": tool_id,
            "request_id": command_request_id,
            "force_closed": True,
            "force_closed_process_ids": sorted(force_closed_process_ids),
            "remaining_process_ids": [],
            "message": "Tool process tree force-closed; no background process remains",
        }

    async def shutdown_tool_backend(
        self,
        tool_id: str,
        *,
        reason: str = "hot-update",
    ) -> Dict[str, Any]:
        """Stop a validated standalone backend so updated Python is reloaded."""

        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", tool_id):
            return {"ok": False, "error_code": "INVALID_TOOL_ID"}
        try:
            self._authorize_tool_lifecycle(tool_id, "stop")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        try:
            standalone_root = self._tool_directory_for_id(tool_id)
        except ValueError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "TOOL_NOT_FOUND",
                "message": "Tool directory is unavailable",
            }
        ipc_root = standalone_root / "runtime" / "ipc"
        owner_path = ipc_root / f"standalone-{tool_id}-backend.json"
        token_path = ipc_root / "session-token"
        try:
            for candidate in (standalone_root, ipc_root, owner_path, token_path):
                metadata = candidate.lstat()
                attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
                if stat_module.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400):
                    raise ValueError("standalone backend descriptor path is linked")
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            pid = int(owner.get("pid") or 0)
            port = int(owner.get("backend_port") or 0)
            shutdown_token = str(owner.get("shutdown_token") or "").strip()
            expected_root = self.project_root.resolve()
            expected_instance = hashlib.sha256(
                os.path.normcase(str(expected_root)).replace("\\", "/").encode("utf-8")
            ).hexdigest()[:24]
            if (
                str(owner.get("tool_id") or "") != tool_id
                or Path(str(owner.get("project_root") or "")).resolve()
                != expected_root
                or str(owner.get("workspace_instance_id") or "")
                != expected_instance
                or pid <= 0
                or not 1024 <= port <= 65535
                or not re.fullmatch(r"[a-f0-9]{64}", shutdown_token)
            ):
                raise ValueError("standalone backend descriptor is invalid")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": True, "tool_id": tool_id, "backend_running": False}

        def request_shutdown() -> None:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/shutdown",
                data=b"",
                headers={
                    "X-GPTBridge-Shutdown-Token": shutdown_token,
                    "X-GPTBridge-Shutdown-Reason": reason,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5):
                    pass
            except (OSError, urllib.error.URLError):
                pass

        await asyncio.to_thread(request_shutdown)
        expected_main = (
            standalone_root
            / "dist"
            / "resources"
            / "app"
            / "independent_tool"
            / tool_id
            / "src"
            / "channel_runtime.py"
        )
        for _ in range(20):
            if not self._validated_standalone_backend_pid(
                pid,
                expected_main,
            ):
                return {"ok": True, "tool_id": tool_id, "backend_stopped": True}
            await asyncio.sleep(0.25)

        if os.name == "nt" and self._validated_standalone_backend_pid(
            pid,
            expected_main,
        ):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **_background_subprocess_kwargs(),
            )
            await killer.communicate()
        if self._validated_standalone_backend_pid(
            pid,
            expected_main,
        ):
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "BACKEND_SHUTDOWN_FAILED",
            }
        return {"ok": True, "tool_id": tool_id, "backend_stopped": True}

    @staticmethod
    def _validated_standalone_backend_pid(pid: int, expected_main: Path) -> bool:
        if os.name != "nt" or pid <= 0:
            return False
        env = os.environ.copy()
        env["GPTBRIDGE_BACKEND_PID"] = str(pid)
        env["GPTBRIDGE_EXPECTED_MAIN"] = str(expected_main.resolve(strict=False))
        command = (
            "$pidValue=[int]$env:GPTBRIDGE_BACKEND_PID;"
            "$expected=[IO.Path]::GetFullPath($env:GPTBRIDGE_EXPECTED_MAIN);"
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$pidValue\" "
            "-ErrorAction SilentlyContinue;"
            "if($p -and $p.CommandLine -and "
            "$p.CommandLine.IndexOf($expected,[StringComparison]::OrdinalIgnoreCase) -ge 0){'1'}"
        )
        try:
            completed = _run_hidden_subprocess(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                env=env,
            )
        except Exception:
            return False
        return completed.returncode == 0 and completed.stdout.strip() == "1"

    async def request_tool_execution(
        self,
        payload: Dict[str, Any],
        event_callback: ToolEventCallback | None = None,
    ) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}
        if (
            self.allowed_tool_ids is not None
            and tool_id not in self.allowed_tool_ids
        ):
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "TOOL_OUTSIDE_STANDALONE_SCOPE",
                "message": "This standalone runtime can execute only its own tool.",
            }
        try:
            if self.governance is None:
                raise PermissionError("PERMISSION_DENIED")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }

        # On-demand start: if the tool was idle-stopped, auto-start it before
        # queuing the execution request so there is a process to pick it up.
        if tool_id not in self._started_request_by_tool:
            try:
                start_result = await self.start_tool(
                    {
                        "tool_id": tool_id,
                        "request_id": f"on-demand-{tool_id}-{time.time_ns()}",
                        "background": True,
                    }
                )
            except Exception:
                start_result = {"ok": False}
            if start_result.get("ok") is not True:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "error_code": "ON_DEMAND_START_FAILED",
                    "message": start_result.get("message", "ON_DEMAND_START_FAILED"),
                    "start_result": start_result,
                }

        # Notify idle manager of activity (resets the idle timer).
        if self._tool_activity_callback is not None:
            try:
                self._tool_activity_callback(tool_id)
            except Exception:
                pass

        request_id, request_error = self._tool_request_id(payload)
        if request_error is not None or request_id is None:
            return {"tool_id": tool_id, **(request_error or {})}

        try:
            self.permission_sovereign.submit_tool_execution_request(
                tool_id,
                request_id,
                dict(payload),
            )
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        return {
            "ok": True,
            "queued": True,
            "tool_id": tool_id,
            "request_id": request_id,
            "status": "queued",
            "channel": "shared-layer",
        }

    async def cancel_tool_execution(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        try:
            if self.governance is None:
                raise PermissionError("PERMISSION_DENIED")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        requested_id = str(payload.get("request_id", "")).strip()
        if not requested_id:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MISSING_REQUEST_ID",
                "message": "Cancellation requires the exact request_id",
            }
        if len(requested_id) > MAX_TOOL_REQUEST_ID_LENGTH or any(
            ord(character) < 32 for character in requested_id
        ):
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": requested_id,
                "error_code": "INVALID_REQUEST_ID",
                "message": "request_id is too long",
            }

        try:
            cancelled = self.permission_sovereign.cancel_tool_execution_request(
                tool_id,
                requested_id,
            )
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": requested_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        return {
            "ok": cancelled,
            "tool_id": tool_id,
            "request_id": requested_id,
            "status": "cancelled" if cancelled else "not-found",
            "channel": "shared-layer",
            "error_code": None if cancelled else "REQUEST_NOT_FOUND",
        }
