from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import stat as stat_module
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict
from managers.process_utils import terminate_process_tree
from .package_integrity import load_package_metadata, verify_packaged_app
from .toolbox_repository import ToolboxRepository

ToolEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]

MAX_TOOL_ARGUMENTS = 256
MAX_TOOL_ARGUMENT_BYTES = 256 * 1024
MAX_TOOL_REQUEST_ID_LENGTH = 128
MAX_TOOL_OUTPUT_CHARS = 2 * 1024 * 1024
MAX_TOOL_STREAM_LINE_BYTES = 4 * 1024 * 1024
_TRUSTED_MANAGED_BACKEND_REUSE_ENV = (
    "GPTBRIDGE_TRUSTED_MANAGED_BACKEND_REUSE"
)
_MANAGED_BACKEND_TOOL_ID_ENV = "GPTBRIDGE_MANAGED_BACKEND_TOOL_ID"
_MANAGED_BACKEND_WORKSPACE_ID_ENV = "GPTBRIDGE_MANAGED_BACKEND_WORKSPACE_INSTANCE_ID"
_MANAGED_BACKEND_VERSION_ENV = "GPTBRIDGE_MANAGED_BACKEND_VERSION"

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
_TOOL_DECLARABLE_ENVIRONMENT_KEYS = frozenset(
    {
        "ALPHAVANTAGE_API_KEY",
        "FILE_SORTER_STATE_ROOT",
        "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT",
        "GPTBRIDGE_AI_ASSISTANT_PROFILE",
        "GPTBRIDGE_DISABLE_LOCAL_LLM",
        "GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ALLOW_LAN",
        "GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ENABLED",
        "GPTBRIDGE_INVESTMENT_MOBILE_SYNC_PORT",
        "GPTBRIDGE_INVESTMENT_MOBILE_SYNC_URL",
        "GPTBRIDGE_LOCAL_AI_WS_URL",
        "GPTBRIDGE_EXTERNAL_AI_WS_URL",
        "GPTBRIDGE_CLEANER_TARGET_ROOT",
        "GPTBRIDGE_LOCAL_LLM_MODEL",
        "GPTBRIDGE_LOCAL_LLM_NUM_CTX",
        "GPTBRIDGE_LOCAL_LLM_NUM_PREDICT",
        "GPTBRIDGE_LOCAL_LLM_PROFILE",
        "GPTBRIDGE_LOCAL_LLM_URL",
    }
)


def _background_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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
    """Service for managing platform tools."""
    
    def __init__(
        self,
        project_root: Path,
        enforcer: Any = None,
        *,
        allowed_tool_ids: set[str] | frozenset[str] | None = None,
    ):
        self.project_root = project_root
        self.tools_dir = self.project_root / "platform_tools"
        self.enforcer = enforcer
        self.allowed_tool_ids = (
            None
            if allowed_tool_ids is None
            else frozenset(str(item).strip() for item in allowed_tool_ids)
        )
        self.repository = ToolboxRepository(project_root)
        # Process ownership is request-scoped. The auxiliary tool index prevents
        # two jobs from mutating the same tool workspace at the same time.
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}
        self._request_tool_ids: dict[str, str] = {}
        self._request_kinds: dict[str, str] = {}
        self._active_request_by_tool: dict[str, str] = {}
        self._started_request_by_tool: dict[str, str] = {}
        self._cancelled_request_ids: set[str] = set()
        self._process_state_lock = asyncio.Lock()

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
        try:
            if path.is_symlink():
                return True
            info = path.stat(follow_symlinks=False)
            attributes = int(getattr(info, "st_file_attributes", 0))
            reparse_flag = int(
                getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            return bool(reparse_flag and attributes & reparse_flag)
        except OSError:
            # Metadata failures are not proof of safety. Callers must reject
            # inaccessible paths rather than treating them as ordinary files.
            return True

    def _validated_tool_directory(self, tool_dir: Path) -> Path:
        tools_root = self.tools_dir.resolve()
        raw_tool_dir = Path(tool_dir)
        try:
            raw_tool_dir.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ValueError("Tool directory metadata could not be verified") from error
        else:
            if self._is_link_or_reparse_point(raw_tool_dir):
                raise ValueError("Tool directory cannot be a link or reparse point")
        resolved = raw_tool_dir.resolve()
        try:
            relative = resolved.relative_to(tools_root)
        except ValueError as error:
            raise ValueError("Tool directory escaped platform_tools") from error
        if len(relative.parts) != 1:
            raise ValueError("Tool directory must be a direct platform_tools child")
        return resolved

    def _validated_tool_path(
        self,
        tool_dir: Path,
        candidate: Path,
        *,
        label: str,
    ) -> Path:
        tool_root = self._validated_tool_directory(tool_dir)
        lexical_root = Path(os.path.abspath(tool_dir))
        lexical_candidate = Path(os.path.abspath(candidate))
        try:
            lexical_relative = lexical_candidate.relative_to(lexical_root)
        except ValueError as error:
            raise ValueError(f"{label} escaped the tool directory") from error

        current = lexical_root
        for part in lexical_relative.parts:
            current = current / part
            try:
                current.lstat()
            except FileNotFoundError:
                # A not-yet-created output has no filesystem object to follow;
                # every existing ancestor up to this point was validated.
                break
            except OSError as error:
                raise ValueError(f"{label} metadata could not be verified") from error
            if self._is_link_or_reparse_point(current):
                raise ValueError(f"{label} traversed a link or reparse point")

        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(tool_root)
        except ValueError as error:
            raise ValueError(f"{label} escaped the tool directory") from error
        return resolved

    def _resolve_entry_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        tool_root = self._validated_tool_directory(tool_dir)
        runtime = manifest.get("runtime")
        if isinstance(runtime, dict):
            runtime_entry = str(runtime.get("entry", "")).strip()
            if runtime_entry:
                entry_path = tool_root / Path(runtime_entry)
                if entry_path.suffix == "":
                    entry_path = entry_path.with_suffix(".py")
                return self._validated_tool_path(
                    tool_root,
                    entry_path,
                    label="Tool runtime entry",
                )

        entry = str(manifest.get("entry", "")).strip()
        if entry:
            entry_path = self.project_root / Path(entry)
            if entry_path.suffix == "":
                entry_path = entry_path.with_suffix(".py")
            return self._validated_tool_path(
                tool_root,
                entry_path,
                label="Tool entry",
            )
        return self._validated_tool_path(
            tool_root,
            tool_root / "src" / "main.py",
            label="Tool entry",
        )

    def _resolve_working_directory(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        tool_root = self._validated_tool_directory(tool_dir)
        runtime = manifest.get("runtime")
        raw_cwd = "."
        if isinstance(runtime, dict):
            raw_cwd = str(runtime.get("workingDirectory", ".")).strip() or "."
        cwd = self._validated_tool_path(
            tool_root,
            tool_root / raw_cwd,
            label="Tool working directory",
        )
        if not cwd.is_dir():
            raise ValueError("Tool working directory does not exist")
        return cwd

    def _resolve_executable_file(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        tool_root = self._validated_tool_directory(tool_dir)
        executable = manifest.get("executable")
        raw_path = ""
        if isinstance(executable, dict):
            raw_path = str(executable.get("path", "")).strip()
        if not raw_path:
            tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
            raw_path = f"dist/{tool_id}.exe"
        return self._validated_tool_path(
            tool_root,
            tool_root / raw_path,
            label="Tool executable",
        )

    def _resolve_python_executable(self, manifest: Dict[str, Any], tool_dir: Path) -> Path:
        tool_root = self._validated_tool_directory(tool_dir)
        runtime = manifest.get("runtime")
        candidates: list[Path] = []
        if isinstance(runtime, dict):
            raw_python = str(runtime.get("python", "")).strip()
            if raw_python:
                python_path = Path(raw_python)
                if python_path.is_absolute():
                    resolved_python = python_path.resolve()
                    if resolved_python != Path(sys.executable).resolve():
                        raise ValueError(
                            "Absolute runtime Python must be the current interpreter"
                        )
                    candidates.append(resolved_python)
                else:
                    if self.allowed_tool_ids is not None:
                        raise ValueError(
                            "Standalone tools cannot select an unverified "
                            "tool-local Python environment"
                        )
                    candidates.append(
                        self._validated_tool_path(
                            tool_root,
                            tool_root / python_path,
                            label="Tool runtime Python",
                        )
                    )
        if self.allowed_tool_ids is None:
            candidates.extend(
                [
                    tool_root / ".venv" / "Scripts" / "python.exe",
                    tool_root / ".venv" / "bin" / "python",
                ]
            )
        candidates.append(Path(sys.executable))
        for candidate in candidates:
            if candidate.exists():
                if candidate != Path(sys.executable) and self._is_link_or_reparse_point(
                    candidate
                ):
                    raise ValueError(
                        "Tool runtime Python cannot be a link or reparse point"
                    )
                return candidate
        return Path(sys.executable)

    def _forget_missing_tool(self, tool_id: str) -> None:
        if re.match(r"^[a-z0-9_-]+$", tool_id):
            self.repository.tombstone_tool(
                tool_id,
                reason="tool_directory_missing",
            )

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
        trusted_managed_backend_reuse: bool = False,
        start_hidden: bool = False,
    ) -> dict[str, str]:
        environment = manifest.get("environment") if isinstance(manifest, dict) else None
        declared_keys = environment.get("allow", []) if isinstance(environment, dict) else []
        if not isinstance(declared_keys, list):
            declared_keys = []
        tool_keys = {
            str(key).strip().upper()
            for key in declared_keys
            if str(key).strip().upper() in _TOOL_DECLARABLE_ENVIRONMENT_KEYS
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
        child_env["GPTBRIDGE_PROJECT_ROOT"] = str(isolated_tool_root)
        child_env["GPTBRIDGE_TOOL_ID"] = tool_id
        child_env["GPTBRIDGE_TOOL_DIR"] = str(isolated_tool_root)
        child_env["GPTBRIDGE_TOOL_DATA_ROOT"] = str(isolated_data_root)
        child_env["GPTBRIDGE_TOOL_DATABASE_ROOT"] = str(
            isolated_data_root / "state"
        )
        child_env["GPTBRIDGE_MANAGED_STORAGE_ROOT"] = str(
            self.project_root.resolve()
            / "platform_tools"
            / "project-cleaner"
            / "data"
        )
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
                if key not in _TOOL_DECLARABLE_ENVIRONMENT_KEYS:
                    continue
                # A binding is controlled by the manifest, never by an
                # ambient parent-process value with the same name.
                child_env.pop(key, None)
                if source == "project_root":
                    capabilities = manifest.get("capabilities", {}) if isinstance(manifest, dict) else {}
                    permissions = manifest.get("permissions", {}) if isinstance(manifest, dict) else {}
                    has_project_authority = (
                        isinstance(capabilities, dict)
                        and any(
                            isinstance(capability, dict)
                            and capability.get("authority") == "project-root-only"
                            for capability in capabilities.values()
                        )
                        and isinstance(permissions, dict)
                        and "authorized-project" in (permissions.get("allow_modify") or [])
                    )
                    if not has_project_authority:
                        continue
                    binding_root = self.project_root.resolve()
                    inherited_root = self._validated_inherited_project_binding(tool_id, key)
                    if inherited_root is not None:
                        binding_root = inherited_root
                    child_env[key] = str(binding_root)
                elif source == "tool_root":
                    child_env[key] = str(tool_dir.resolve())
        managed_keys = (
            _TRUSTED_MANAGED_BACKEND_REUSE_ENV,
            _MANAGED_BACKEND_TOOL_ID_ENV,
            _MANAGED_BACKEND_WORKSPACE_ID_ENV,
            _MANAGED_BACKEND_VERSION_ENV,
        )
        for key in managed_keys:
            child_env.pop(key, None)
        if trusted_managed_backend_reuse:
            normalized_root = str(self.project_root.resolve()).replace("\\", "/")
            if os.name == "nt":
                normalized_root = normalized_root.lower()
            workspace_instance_id = hashlib.sha256(
                normalized_root.encode("utf-8")
            ).hexdigest()[:24]
            version = (
                str(manifest.get("version", "")).strip()
                if isinstance(manifest, dict)
                else ""
            )
            child_env[_TRUSTED_MANAGED_BACKEND_REUSE_ENV] = "1"
            child_env[_MANAGED_BACKEND_TOOL_ID_ENV] = tool_id
            child_env[_MANAGED_BACKEND_WORKSPACE_ID_ENV] = workspace_instance_id
            child_env[_MANAGED_BACKEND_VERSION_ENV] = version
        self._inject_declared_peer_connections(child_env, manifest)
        return child_env

    def _inject_declared_peer_connections(
        self,
        child_env: dict[str, str],
        manifest: Dict[str, Any] | None,
    ) -> None:
        """Inject only manifest-authorized, currently available AI peer sessions.

        The main process acts as a lifecycle capability broker. It reads the
        private loopback session descriptor at launch time but never persists,
        proxies, or shares a peer database. A stale or missing descriptor
        yields no binding, so the caller fails immediately instead of queuing.
        """

        capabilities = manifest.get("capabilities") if isinstance(manifest, dict) else None
        connection = (
            capabilities.get("ai-connections")
            if isinstance(capabilities, dict)
            else None
        )
        if not isinstance(connection, dict):
            return
        peers = connection.get("peers")
        bindings = connection.get("bindings")
        environment = manifest.get("environment") if isinstance(manifest, dict) else None
        declared_environment = {
            str(key).strip().upper()
            for key in (environment.get("allow") or [])
        } if isinstance(environment, dict) else set()
        if not isinstance(peers, list) or not isinstance(bindings, dict):
            return
        for raw_peer_id in peers:
            peer_id = str(raw_peer_id or "").strip()
            environment_key = str(bindings.get(peer_id) or "").strip().upper()
            if (
                not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", peer_id)
                or environment_key not in _TOOL_DECLARABLE_ENVIRONMENT_KEYS
                or environment_key not in declared_environment
            ):
                continue
            peer_url = self._active_peer_websocket_url(peer_id)
            if peer_url:
                child_env[environment_key] = peer_url

    def _active_peer_websocket_url(self, peer_id: str) -> str:
        manifest_path = self.tools_dir / peer_id / "manifest.json"
        try:
            peer_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                str(peer_manifest.get("id") or "") != peer_id
                or peer_manifest.get("enabled", True) is False
            ):
                return ""
            local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
            state_base = (
                Path(local_app_data)
                if local_app_data
                else Path.home() / "AppData" / "Local"
            )
            peer_root = (
                state_base / "GPTBridge" / "standalone" / peer_id
            ).resolve()
            ipc_root = peer_root / "runtime" / "ipc"
            owner_path = ipc_root / f"standalone-{peer_id}-backend.json"
            token_path = ipc_root / "session-token"
            for candidate in (peer_root, ipc_root, owner_path, token_path):
                value = candidate.lstat()
                attributes = int(getattr(value, "st_file_attributes", 0) or 0)
                if stat_module.S_ISLNK(value.st_mode) or bool(attributes & 0x400):
                    return ""
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            token = token_path.read_text(encoding="utf-8").strip().lower()
            port = int(owner.get("backend_port") or 0)
            expected_instance = hashlib.sha256(
                os.path.normcase(str(peer_root)).replace("\\", "/").encode("utf-8")
            ).hexdigest()[:24]
            if (
                str(owner.get("tool_id") or "") != peer_id
                or Path(str(owner.get("project_root") or "")).resolve() != peer_root
                or str(owner.get("workspace_instance_id") or "") != expected_instance
                or not 1024 <= port <= 65535
                or not re.fullmatch(r"[a-f0-9]{64}", token)
            ):
                return ""
            return (
                f"ws://127.0.0.1:{port}/?token={token}"
                f"&instance={expected_instance}"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return ""

    def _validated_inherited_project_binding(
        self,
        tool_id: str,
        environment_key: str,
    ) -> Path | None:
        """Preserve a wrapper-authorized host root inside standalone backends.

        A packaged tool runs its backend from an isolated installation root.
        The Electron wrapper validates declared project-root bindings before
        starting that backend.  Rebinding the same variable to the isolated
        root here would silently revoke the cleaner's host-project authority.
        Main-runtime services never inherit this value; only a tool-scoped
        standalone backend may preserve it, and the target must identify the
        same tool in a structurally valid GPTBridge project.
        """

        if self.allowed_tool_ids is None or tool_id not in self.allowed_tool_ids:
            return None
        configured = str(os.environ.get(environment_key, "")).strip()
        if not configured:
            return None
        try:
            candidate = Path(configured).resolve(strict=True)
            if not (candidate / "package.json").is_file():
                return None
            manifest_path = candidate / "platform_tools" / tool_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if str(manifest.get("id", "")).strip() != tool_id:
            return None
        declaration = manifest.get("environment")
        bindings = (
            declaration.get("bindings")
            if isinstance(declaration, dict)
            else None
        )
        if not isinstance(bindings, dict):
            return None
        declared_source = next(
            (
                source
                for raw_key, source in bindings.items()
                if str(raw_key).strip().upper() == environment_key
            ),
            None,
        )
        return candidate if declared_source == "project_root" else None

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

    @staticmethod
    def _directory_size_bytes(tool_dir: Path) -> int:
        total = 0
        if not tool_dir.exists() or not tool_dir.is_dir():
            return total

        for root, dirs, files in os.walk(tool_dir):
            for filename in files:
                path = Path(root) / filename
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    @classmethod
    def _project_size_bytes(cls, tool_dir: Path) -> int:
        return cls._directory_size_bytes(tool_dir)

    def _manifest_to_record(self, tool_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
        manifest_path = tool_dir / "manifest.json"
        entry_file = self._resolve_entry_file(manifest, tool_dir)
        executable_file = self._resolve_executable_file(manifest, tool_dir)
        record = dict(manifest)
        record["folder_path"] = str(tool_dir)
        record["manifest_path"] = str(manifest_path)
        record["code_path"] = str(entry_file)
        record["standalone"] = True
        record["executable_path"] = str(executable_file)
        record["executable_exists"] = executable_file.exists()
        record["project_size_bytes"] = self._project_size_bytes(tool_dir)
        return record

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
        return records

    def _sync_database_from_manifests(self) -> list[Dict[str, Any]]:
        records = self._load_manifest_records()
        self.repository.replace_tools(records)
        return records

    async def list_tools(self) -> Dict[str, Any]:
        manifest_records = self._sync_database_from_manifests()
        tools = self.repository.list_tools()
        stored_by_id = {
            str(tool.get("id", "")).strip(): tool
            for tool in tools
            if str(tool.get("id", "")).strip()
        }
        for manifest_record in manifest_records:
            tool_id = str(manifest_record.get("id", "")).strip()
            executable_path = str(
                manifest_record.get("executable_path", "")
            ).strip()
            if not tool_id or not executable_path:
                continue
            running = bool(
                self._running_executable_process_ids(Path(executable_path))
            )
            stored_status = str(
                stored_by_id.get(tool_id, {}).get("status", "stopped")
            ).strip()
            if running and stored_status != "running":
                self.repository.update_status(tool_id, "running")
            elif not running and stored_status in {"running", "starting", "stopping"}:
                self.repository.update_status(tool_id, "stopped")
        tools = self.repository.list_tools()
        records_by_id = {
            str(record.get("id", "")).strip(): record
            for record in manifest_records
            if str(record.get("id", "")).strip()
        }
        for tool in tools:
            tool_id = str(tool.get("id", "")).strip()
            manifest_record = records_by_id.get(tool_id, {})
            for field in (
                "folder_path",
                "manifest_path",
                "code_path",
                "standalone",
                "executable_path",
                "executable_exists",
            ):
                if field in manifest_record:
                    tool[field] = manifest_record[field]
            manifest_path = Path(str(tool.get("manifest_path", "")))
            if manifest_path.name:
                folder_path = manifest_path.parent
                tool["folder_path"] = str(folder_path)
            elif tool_id:
                folder_path = self.tools_dir / tool_id
                tool["folder_path"] = str(folder_path)
            else:
                folder_path = None
            if folder_path is not None:
                tool["project_size_bytes"] = self._project_size_bytes(folder_path)
        return {"ok": True, "tools": tools, "database_path": str(self.repository.db_path)}

    async def update_status(self, tool_id: str, status: str) -> Dict[str, Any]:
        manifest_path = self.tools_dir / tool_id / "manifest.json"
        if not manifest_path.exists():
            self._forget_missing_tool(tool_id)
            return self._missing_tool_result(tool_id)

        updated_database = self.repository.update_status(tool_id, status)
        if updated_database:
            return {"ok": True}

        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.repository.upsert_tool(self._manifest_to_record(manifest_path.parent, manifest))
                if self.repository.update_status(tool_id, status):
                    return {"ok": True}
            except Exception as e:
                return {"ok": False, "message": str(e)}

        return {"ok": False, "message": "Tool not found"}

    @staticmethod
    def _running_executable_process_ids(executable_file: Path) -> list[int]:
        if os.name != "nt":
            return []

        env = os.environ.copy()
        env["GPTBRIDGE_EXECUTABLE_PATH"] = str(executable_file.resolve())
        command = (
            "$target = [System.IO.Path]::GetFullPath($env:GPTBRIDGE_EXECUTABLE_PATH);"
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.ExecutablePath -and "
            "([System.IO.Path]::GetFullPath($_.ExecutablePath)).Equals("
            "$target, [System.StringComparison]::OrdinalIgnoreCase) } | "
            "Select-Object -ExpandProperty ProcessId"
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
            return []

        if completed.returncode != 0:
            return []

        process_ids: list[int] = []
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                process_ids.append(int(line))
            except ValueError:
                continue
        return process_ids

    @staticmethod
    def _stop_running_executable(executable_file: Path) -> list[int]:
        process_ids = ToolboxService._running_executable_process_ids(executable_file)
        if os.name != "nt" or not process_ids:
            return []

        env = os.environ.copy()
        env["GPTBRIDGE_PROCESS_IDS"] = ",".join(str(pid) for pid in process_ids)
        command = (
            "$ids = $env:GPTBRIDGE_PROCESS_IDS -split ',' | "
            "Where-Object { $_ } | ForEach-Object { [int]$_ };"
            "foreach ($id in $ids) { "
            "Stop-Process -Id $id -Force -ErrorAction SilentlyContinue "
            "};"
            "$ids"
        )

        try:
            _run_hidden_subprocess(
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
            return []
        return process_ids

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
                    trusted_managed_backend_reuse=False,
                ),
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
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}

        request_id, request_error = self._tool_request_id(payload)
        if request_error is not None or request_id is None:
            return {"tool_id": tool_id, **(request_error or {})}
        args, argument_error = self._tool_arguments(payload, tool_id)
        if argument_error is not None or args is None:
            if argument_error is not None:
                argument_error["request_id"] = request_id
            return argument_error or {"ok": False, "tool_id": tool_id, "request_id": request_id}

        try:
            tool_dir = self._validated_tool_directory(self.tools_dir / tool_id)
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
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "tool_id": tool_id, "message": f"Invalid tool manifest: {exc}"}

        if manifest.get("enabled", True) is False:
            return {"ok": False, "tool_id": tool_id, "message": "Tool is disabled"}

        try:
            executable_file = self._resolve_executable_file(manifest, tool_dir)
        except ValueError as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "INVALID_EXECUTABLE_PATH",
                "message": str(error),
            }
        if not executable_file.exists():
            await self.update_status(tool_id, "stopped")
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "message": f"Standalone EXE not found. Run npm run package:tool -- {tool_id}",
                "executable_path": str(executable_file),
            }

        package_check = verify_packaged_app(
            executable_file.parent / "resources" / "app",
        )
        if not package_check.get("ok"):
            await self.update_status(tool_id, "error")
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": str(
                    package_check.get("error_code", "PACKAGE_UNVERIFIED")
                ),
                "message": (
                    f"{package_check.get('message', 'Package verification failed')}. "
                    f"Run npm run package:tool -- {tool_id}"
                ),
                "executable_path": str(executable_file),
            }
        package_metadata = load_package_metadata(
            executable_file.parent / "resources" / "app"
        )
        source_version = str(manifest.get("version") or "").strip()
        packaged_version = str(
            package_metadata.get("tool_version") or source_version
        ).strip()
        if not source_version or packaged_version != source_version:
            await self.update_status(tool_id, "error")
            return {
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
        try:
            contract = json.loads(
                (
                    self.project_root
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
        if not minimum_contract <= packaged_contract <= current_contract:
            await self.update_status(tool_id, "error")
            return {
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

        tracked_request_id, tracked_process = await self._started_tool_process(tool_id)
        if (
            tracked_request_id is not None
            and tracked_process is not None
            and tracked_process.returncode is None
        ):
            if background:
                status_result = await self.update_status(tool_id, "running")
                if not status_result.get("ok"):
                    return status_result
                return {
                    "ok": True,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "pid": tracked_process.pid,
                    "executable_path": str(executable_file),
                    "background": True,
                    "message": "Tool executable is already running in background",
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

        running_process_ids = self._running_executable_process_ids(executable_file)
        if running_process_ids:
            await self._release_tool_process(request_id)
            if background:
                status_result = await self.update_status(tool_id, "running")
                if not status_result.get("ok"):
                    return status_result
                return {
                    "ok": True,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "pid": running_process_ids[0],
                    "executable_path": str(executable_file),
                    "background": True,
                    "message": "Tool executable is already running in background",
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
                    trusted_managed_backend_reuse=False,
                    start_hidden=background,
                ),
            )
        except Exception as exc:
            await self._release_tool_process(request_id)
            await self.update_status(tool_id, "error")
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "PROCESS_START_FAILED",
                "message": str(exc),
            }

        cancel_pending = await self._register_tool_process(request_id, process)
        asyncio.create_task(
            self._watch_started_tool(
                request_id,
                tool_id,
                executable_file,
                process,
            )
        )
        await self._release_started_tool_command_slot(request_id)
        if cancel_pending and process.returncode is None:
            await terminate_process_tree(process)
        status_result = await self.update_status(tool_id, "running")
        if not status_result.get("ok"):
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "pid": process.pid,
                "executable_path": str(executable_file),
                "status_warning": str(status_result.get("message", "status update failed")),
                "message": "Standalone tool executable started",
            }
        return {
            "ok": True,
            "tool_id": tool_id,
            "request_id": request_id,
            "pid": process.pid,
            "executable_path": str(executable_file),
            "background": background,
            "message": "Standalone tool executable started",
        }

    async def _watch_started_tool(
        self,
        request_id: str,
        tool_id: str,
        executable_file: Path,
        process: asyncio.subprocess.Process,
    ) -> None:
        try:
            await process.wait()
        finally:
            cancelled = await self._release_tool_process(request_id)
            still_running = bool(
                self._running_executable_process_ids(executable_file)
            )
            await self.update_status(
                tool_id,
                "running" if still_running and not cancelled else "stopped",
            )

    async def stop_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}

        request_id, process, _kind = await self._active_tool_process(tool_id)
        if request_id is not None:
            async with self._process_state_lock:
                self._cancelled_request_ids.add(request_id)
        if process is not None and process.returncode is None:
            await terminate_process_tree(process)

        started_request_id, started_process = await self._started_tool_process(tool_id)
        if started_request_id is not None:
            async with self._process_state_lock:
                self._cancelled_request_ids.add(started_request_id)
        if started_process is not None and started_process.returncode is None:
            await terminate_process_tree(started_process)

        try:
            tool_dir = self._validated_tool_directory(self.tools_dir / tool_id)
        except ValueError as error:
            return failure(str(error), "INVALID_TOOL_PATH")
        manifest_path = tool_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                executable_file = self._resolve_executable_file(manifest, tool_dir)
                self._stop_running_executable(executable_file)
            except Exception:
                pass
        else:
            self._forget_missing_tool(tool_id)
            return {
                "ok": True,
                "tool_id": tool_id,
                "message": "舊應用程式資料已移除。",
                "removed": True,
            }

        status_result = await self.update_status(tool_id, "stopped")
        if not status_result.get("ok"):
            return status_result
        result = {"ok": True, "tool_id": tool_id, "message": "Standalone tool executable stopped"}
        if request_id is not None:
            result["request_id"] = request_id
        return result

    async def shutdown_tool_backend(
        self,
        tool_id: str,
        *,
        reason: str = "hot-reload",
    ) -> Dict[str, Any]:
        """Stop a validated standalone backend so updated Python is reloaded."""

        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", tool_id):
            return {"ok": False, "error_code": "INVALID_TOOL_ID"}
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        state_base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
        standalone_root = (
            state_base / "GPTBridge" / "standalone" / tool_id
        ).resolve()
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
            expected_instance = hashlib.sha256(
                os.path.normcase(str(standalone_root)).replace("\\", "/").encode("utf-8")
            ).hexdigest()[:24]
            if (
                str(owner.get("tool_id") or "") != tool_id
                or Path(str(owner.get("project_root") or "")).resolve()
                != standalone_root
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
        for _ in range(20):
            if not self._validated_standalone_backend_pid(
                pid,
                standalone_root / "src-core" / "main.py",
            ):
                return {"ok": True, "tool_id": tool_id, "backend_stopped": True}
            await asyncio.sleep(0.25)

        if os.name == "nt" and self._validated_standalone_backend_pid(
            pid,
            standalone_root / "src-core" / "main.py",
        ):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.communicate()
        if self._validated_standalone_backend_pid(
            pid,
            standalone_root / "src-core" / "main.py",
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

    async def run_tool(
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

        request_id, request_error = self._tool_request_id(payload)
        if request_error is not None or request_id is None:
            return {"tool_id": tool_id, **(request_error or {})}

        def failure(message: str, error_code: str, **details: Any) -> Dict[str, Any]:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "status": "failed",
                "error_code": error_code,
                "message": message,
                **details,
            }

        try:
            tool_dir = self._validated_tool_directory(self.tools_dir / tool_id)
        except ValueError as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "INVALID_TOOL_PATH",
                "message": str(error),
            }
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            self._forget_missing_tool(tool_id)
            return {**self._missing_tool_result(tool_id), "request_id": request_id}

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return failure(f"Invalid tool manifest: {exc}", "INVALID_MANIFEST")

        if manifest.get("enabled", True) is False:
            return failure("Tool is disabled", "TOOL_DISABLED")

        try:
            entry_file = self._resolve_entry_file(manifest, tool_dir)
        except ValueError as error:
            return failure(str(error), "INVALID_ENTRY_PATH")

        if not entry_file.exists():
            return failure("Tool entry file not found", "ENTRY_NOT_FOUND")

        args, argument_error = self._tool_arguments(payload, tool_id)
        if argument_error is not None or args is None:
            if argument_error is not None:
                argument_error["request_id"] = request_id
                argument_error["status"] = "failed"
            return argument_error or failure("Invalid args", "INVALID_ARGUMENTS")

        try:
            timeout_seconds = int(manifest.get("timeout_seconds", 120))
        except (TypeError, ValueError):
            timeout_seconds = 120
        timeout_seconds = max(1, min(timeout_seconds, 3600))

        def _decode_bytes(b: bytes) -> tuple[str, str]:
            if b is None:
                return "", "utf-8"
            # Try utf-8 first, then cp950 (Traditional Chinese on Windows), then fallback
            try:
                return b.decode("utf-8"), "utf-8"
            except Exception:
                try:
                    return b.decode("cp950"), "cp950"
                except Exception:
                    return b.decode("utf-8", errors="replace"), "utf-8-replace"

        child_env = self._tool_environment(tool_id, tool_dir, manifest)
        try:
            python_executable = self._resolve_python_executable(
                manifest,
                tool_dir,
            )
            working_directory = self._resolve_working_directory(
                manifest,
                tool_dir,
            )
        except ValueError as error:
            return failure(str(error), "INVALID_RUNTIME_PATH")

        reservation_error = await self._reserve_tool_process(
            request_id=request_id,
            tool_id=tool_id,
            kind="run",
        )
        if reservation_error is not None:
            reservation_error["status"] = "rejected"
            return reservation_error
        try:
            process_result = await self._run_tool_streaming(
                tool_id=tool_id,
                entry_file=entry_file,
                python_executable=python_executable,
                working_directory=working_directory,
                args=args,
                child_env=child_env,
                timeout_seconds=timeout_seconds,
                decode_bytes=_decode_bytes,
                request_id=request_id,
                event_callback=event_callback,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._release_tool_process(request_id)
            return failure(str(exc), "PROCESS_EXECUTION_FAILED")

        returncode = process_result["exit_code"]
        stdout = str(process_result["stdout"])
        stderr = str(process_result["stderr"])
        stdout_encoding = str(process_result["stdout_encoding"])
        stderr_encoding = str(process_result["stderr_encoding"])
        stdout_truncated = bool(process_result["stdout_truncated"])
        stderr_truncated = bool(process_result["stderr_truncated"])
        cancelled = bool(process_result["cancelled"])
        timed_out = bool(process_result["timed_out"])
        if timed_out:
            status = "timed_out"
            message = f"Tool timed out after {timeout_seconds} seconds"
            error: Dict[str, Any] | None = {
                "code": "TOOL_TIMEOUT",
                "message": message,
                "timeout_seconds": timeout_seconds,
            }
        elif cancelled:
            status = "cancelled"
            message = "Tool cancelled by user"
            error = {"code": "TOOL_CANCELLED", "message": message}
        elif returncode == 0:
            status = "completed"
            message = "Tool completed"
            error = None
        else:
            status = "failed"
            message = "Tool failed"
            error = {
                "code": "TOOL_EXIT_NONZERO",
                "message": message,
                "exit_code": returncode,
            }

        result = {
            "ok": status == "completed",
            "tool_id": tool_id,
            "request_id": request_id,
            "status": status,
            "exit_code": returncode,
            "cancelled": cancelled,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_encoding": stdout_encoding,
            "stderr_encoding": stderr_encoding,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "output": {
                "stdout": stdout,
                "stderr": stderr,
                "stdout_encoding": stdout_encoding,
                "stderr_encoding": stderr_encoding,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
            "message": message,
        }
        if error is not None:
            result["error"] = error
            result["error_code"] = error["code"]
        return result

    async def cancel_tool_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(payload.get("tool_id", "")).strip()
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

        async with self._process_state_lock:
            request_id = requested_id
            registered_tool_id = self._request_tool_ids.get(request_id)
            if registered_tool_id is None or (
                tool_id and registered_tool_id != tool_id
            ):
                return {
                    "ok": False,
                    "tool_id": tool_id or registered_tool_id or "",
                    "request_id": request_id,
                    "error_code": "RUN_NOT_FOUND",
                    "message": "No running tool process for request_id",
                }
            tool_id = registered_tool_id
            if (
                self.allowed_tool_ids is not None
                and tool_id not in self.allowed_tool_ids
            ):
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "error_code": "TOOL_OUTSIDE_STANDALONE_SCOPE",
                    "message": "The run is outside this standalone tool scope.",
                }
            process = self._running_processes.get(request_id)
            if process is not None and process.returncode is not None:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "error_code": "RUN_NOT_FOUND",
                    "message": "Tool process has already exited",
                }
            self._cancelled_request_ids.add(request_id)

        if process is not None:
            await terminate_process_tree(process)
        return {
            "ok": True,
            "tool_id": tool_id,
            "request_id": request_id,
            "message": "Tool stop requested",
        }

    async def _run_tool_streaming(
        self,
        *,
        tool_id: str,
        entry_file: Path,
        python_executable: Path,
        working_directory: Path,
        args: list[str],
        child_env: dict[str, str],
        timeout_seconds: int,
        decode_bytes: Callable[[bytes], tuple[str, str]],
        request_id: str,
        event_callback: ToolEventCallback | None,
    ) -> Dict[str, Any]:
        progress_prefixes = (
            "FILE_SORTER_CLEANUP_PROGRESS_JSON=",
            "INVESTMENT_MANAGER_PROGRESS_JSON=",
            "PROJECT_CLEANER_PROGRESS_JSON=",
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stdout_encoding = "utf-8"
        stderr_encoding = "utf-8"
        stdout_char_count = 0
        stderr_char_count = 0
        stdout_truncated = False
        stderr_truncated = False

        process = await asyncio.create_subprocess_exec(
            str(python_executable),
            "-B",
            "-s",
            str(entry_file),
            *args,
            cwd=str(working_directory),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            limit=MAX_TOOL_STREAM_LINE_BYTES,
            **_background_subprocess_kwargs(),
        )
        cancel_pending = await self._register_tool_process(request_id, process)
        if cancel_pending and process.returncode is None:
            await terminate_process_tree(process)

        async def read_stdout() -> None:
            nonlocal stdout_char_count, stdout_encoding, stdout_truncated
            assert process.stdout is not None
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError:
                    stdout_truncated = True
                    while await process.stdout.read(64 * 1024):
                        pass
                    break
                if not line:
                    break
                decoded, encoding = decode_bytes(line)
                line_text = decoded.rstrip("\r\n")
                progress_prefix = next(
                    (
                        prefix
                        for prefix in progress_prefixes
                        if line_text.startswith(prefix)
                    ),
                    "",
                )
                if progress_prefix:
                    raw_progress = line_text[len(progress_prefix) :]
                    try:
                        progress = json.loads(raw_progress)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(progress, dict):
                        progress_payload = {
                            "ok": True,
                            "tool_id": tool_id,
                            **progress,
                        }
                        if request_id:
                            progress_payload["request_id"] = request_id
                        if event_callback is not None:
                            try:
                                await event_callback(
                                    "toolbox_run_tool_progress",
                                    progress_payload,
                                )
                            except Exception:
                                # Progress delivery is best-effort and must not
                                # stop draining the child's stdout pipe.
                                pass
                    continue
                stdout_encoding = encoding
                remaining = MAX_TOOL_OUTPUT_CHARS - stdout_char_count
                if remaining > 0:
                    retained = decoded[:remaining]
                    stdout_chunks.append(retained)
                    stdout_char_count += len(retained)
                if len(decoded) > remaining:
                    stdout_truncated = True

        async def read_stderr() -> None:
            nonlocal stderr_char_count, stderr_encoding, stderr_truncated
            assert process.stderr is not None
            while True:
                try:
                    line = await process.stderr.readline()
                except ValueError:
                    stderr_truncated = True
                    while await process.stderr.read(64 * 1024):
                        pass
                    break
                if not line:
                    break
                decoded, encoding = decode_bytes(line)
                stderr_encoding = encoding
                remaining = MAX_TOOL_OUTPUT_CHARS - stderr_char_count
                if remaining > 0:
                    retained = decoded[:remaining]
                    stderr_chunks.append(retained)
                    stderr_char_count += len(retained)
                if len(decoded) > remaining:
                    stderr_truncated = True

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())

        timed_out = False
        returncode: int | None = None
        try:
            try:
                returncode = await asyncio.wait_for(
                    process.wait(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                timed_out = True
                if process.returncode is None:
                    await terminate_process_tree(process)
                returncode = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    await terminate_process_tree(process)
                except Exception:
                    pass
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            cancelled = await self._release_tool_process(request_id)

        return {
            "exit_code": returncode,
            "stdout": "".join(stdout_chunks),
            "stderr": "".join(stderr_chunks),
            "stdout_encoding": stdout_encoding,
            "stderr_encoding": stderr_encoding,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "cancelled": cancelled,
            "timed_out": timed_out,
        }

