from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from packager_base import (
    DEFAULT_BACKEND_PORT,
    PACKAGE_METADATA_NAME,
    _background_subprocess_kwargs,
    load_package_metadata,
)
from packager_inventory import (
    _is_link_or_reparse,
    _persist_package_document,
    _process_is_alive,
    _sha256_regular_file,
)
from packager_metadata import standalone_backend_port


def running_executable_process_ids(executable_file: Path) -> list[int]:
    if os.name != "nt" or not executable_file.exists():
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
    completed = subprocess.run(
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
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **_background_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        return []
    process_ids: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            process_ids.append(int(line.strip()))
        except ValueError:
            continue
    return process_ids


def stop_running_executable_for_upgrade(
    executable_file: Path,
    process_ids: list[int],
    *,
    timeout_seconds: float = 15.0,
) -> bool:
    """Stop only verified processes for one packaged tool before promotion."""

    if os.name != "nt" or not process_ids:
        return not running_executable_process_ids(executable_file)
    expected = set(process_ids)
    current = set(running_executable_process_ids(executable_file))
    targets = sorted(expected & current)
    if not targets:
        return True

    for process_id in targets:
        # Re-resolve the executable immediately before termination so a
        # recycled PID can never broaden an upgrade beyond this tool.
        if process_id not in running_executable_process_ids(executable_file):
            continue
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            **_background_subprocess_kwargs(),
        )

    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if not running_executable_process_ids(executable_file):
            return True
        time.sleep(0.2)
    return not running_executable_process_ids(executable_file)


def restart_packaged_executable(executable_file: Path) -> bool:
    """Restart an upgraded tool without inheriting Codex/Electron test mode."""

    if not executable_file.is_file():
        return False
    environment = os.environ.copy()
    environment.pop("ELECTRON_RUN_AS_NODE", None)
    environment.pop("GPTBRIDGE_START_HIDDEN", None)
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    try:
        subprocess.Popen(
            [str(executable_file.resolve())],
            cwd=str(executable_file.parent.resolve()),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError:
        return False
    return True


def windows_process_owns_listening_port(process_id: int, port: int) -> bool:
    if os.name != "nt" or process_id <= 0 or not (1024 <= port <= 65535):
        return False
    env = os.environ.copy()
    env["GPTBRIDGE_EXPECTED_PROCESS_ID"] = str(process_id)
    env["GPTBRIDGE_EXPECTED_LISTEN_PORT"] = str(port)
    command = (
        "$expectedPid = [int]$env:GPTBRIDGE_EXPECTED_PROCESS_ID;"
        "$expectedPort = [int]$env:GPTBRIDGE_EXPECTED_LISTEN_PORT;"
        "$match = Get-NetTCPConnection -State Listen "
        "-LocalPort $expectedPort -ErrorAction SilentlyContinue | "
        "Where-Object { $_.OwningProcess -eq $expectedPid } | "
        "Select-Object -First 1;"
        "if ($null -ne $match) { 'owned' }"
    )
    completed = subprocess.run(
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
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        **_background_subprocess_kwargs(),
    )
    return completed.returncode == 0 and completed.stdout.strip() == "owned"


def legacy_standalone_backend_owner_path(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        state_base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
        state_base = (
            Path(xdg_state_home)
            if xdg_state_home
            else Path.home() / ".local" / "state"
        )
    return state_base / "GPTBridge" / "ipc" / f"standalone-{safe_tool_id}-backend.json"


def standalone_project_root(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        state_base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
        state_base = (
            Path(xdg_state_home)
            if xdg_state_home
            else Path.home() / ".local" / "state"
        )
    return Path(
        os.path.abspath(state_base / "GPTBridge" / "standalone" / safe_tool_id)
    )


def standalone_backend_owner_path(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    return (
        standalone_project_root(tool_id)
        / "runtime"
        / "ipc"
        / f"standalone-{safe_tool_id}-backend.json"
    )


def workspace_instance_id(project_root: Path) -> str:
    normalized = os.path.normcase(
        os.path.abspath(project_root)
    ).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _force_stop_verified_unresponsive_backend(
    *,
    tool_id: str,
    owner: dict[str, Any],
    owner_path: Path,
    backend_port: int,
    live_app_dir: Path,
) -> bool:
    """Terminate only a hung process proven to belong to this exact package."""

    if os.name != "nt":
        return False
    try:
        process_id = int(owner.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False

    metadata = load_package_metadata(live_app_dir)
    payload_files = metadata.get("payload_files")
    package_digest = str(metadata.get("payload_digest") or "").strip().lower()
    owner_digest = str(owner.get("package_digest") or "").strip().lower()
    expected_runtime_hash = (
        str(payload_files.get("python/python.exe") or "").strip().lower()
        if isinstance(payload_files, dict)
        else ""
    )
    runtime = live_app_dir / "python" / "python.exe"
    if (
        re.fullmatch(r"[0-9a-f]{64}", package_digest) is None
        or owner_digest != package_digest
        or re.fullmatch(r"[0-9a-f]{64}", expected_runtime_hash) is None
        or not runtime.is_file()
        or _is_link_or_reparse(runtime)
        or _sha256_regular_file(runtime) != expected_runtime_hash
        or process_id not in running_executable_process_ids(runtime)
        or not windows_process_owns_listening_port(process_id, backend_port)
    ):
        return False

    expected_root = standalone_project_root(tool_id)
    for controlled_path in (
        expected_root.parent.parent,
        expected_root.parent,
        expected_root,
    ):
        if not controlled_path.exists():
            continue
        if _is_link_or_reparse(controlled_path):
            return False
        if controlled_path.resolve(strict=True) != controlled_path:
            return False

    audit_root = expected_root / "runtime" / "backend-handoff-recovery"
    audit_root.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(audit_root):
        return False
    operation_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-{uuid.uuid4().hex}"
    )
    requested_path = audit_root / f"{operation_id}.requested.json"
    audit_payload = {
        "format_version": 1,
        "status": "force-stop-requested",
        "reason": "verified-packaged-backend-health-timeout",
        "tool_id": tool_id,
        "process_id": process_id,
        "backend_port": backend_port,
        "owner_path": str(owner_path),
        "project_root": str(expected_root),
        "runtime_executable": str(runtime),
        "runtime_sha256": expected_runtime_hash,
        "package_digest": package_digest,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    _persist_package_document(requested_path, audit_payload)

    completed = subprocess.run(
        ["taskkill", "/PID", str(process_id), "/T", "/F"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
        **_background_subprocess_kwargs(),
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (
            not _process_is_alive(process_id)
            and not windows_process_owns_listening_port(
                process_id,
                backend_port,
            )
        ):
            _persist_package_document(
                audit_root / f"{operation_id}.completed.json",
                {
                    **audit_payload,
                    "status": "force-stop-completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "taskkill_exit_code": int(completed.returncode),
                    "request_journal": str(requested_path),
                },
            )
            return True
        time.sleep(0.2)
    raise RuntimeError(
        "Verified packaged backend remained active after bounded force-stop; "
        f"recovery journal: {requested_path}"
    )


def stop_verified_packaged_backend(tool_id: str, live_app_dir: Path) -> bool:
    def valid_port(value: object) -> int | None:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return None
        return port if 1024 <= port <= 65535 else None

    expected_root = standalone_project_root(tool_id)
    expected_instance = workspace_instance_id(expected_root)
    expected_root_text = os.path.normcase(
        os.path.normpath(str(expected_root))
    )

    descriptor_port: int | None = None
    for descriptor_path in (
        live_app_dir / "manifest.json",
        live_app_dir / PACKAGE_METADATA_NAME,
    ):
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(descriptor, dict):
            continue
        if descriptor_path.name == "manifest.json":
            standalone = descriptor.get("standalone")
            port_value = (
                standalone.get("backend_port")
                if isinstance(standalone, dict)
                else None
            )
        else:
            port_value = descriptor.get("backend_port")
        descriptor_port = valid_port(port_value)
        if descriptor_port is not None:
            break

    owner_candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[int, str]] = set()
    owner_paths = (
        standalone_backend_owner_path(tool_id),
        legacy_standalone_backend_owner_path(tool_id),
    )
    for owner_index, owner_path in enumerate(owner_paths):
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(owner, dict):
            continue
        if str(owner.get("tool_id") or "") != tool_id:
            continue
        owner_root_text = str(owner.get("project_root") or "").strip()
        if (
            not owner_root_text
            or not os.path.isabs(owner_root_text)
            or os.path.normcase(os.path.normpath(owner_root_text))
            != expected_root_text
            or str(owner.get("workspace_instance_id") or "")
            != expected_instance
        ):
            continue
        shutdown_token = str(owner.get("shutdown_token") or "")
        if re.fullmatch(r"[0-9a-fA-F]{64}", shutdown_token) is None:
            continue
        backend_port = valid_port(owner.get("backend_port"))
        if backend_port is None:
            backend_port = (
                DEFAULT_BACKEND_PORT
                if owner_index == 1
                else descriptor_port or standalone_backend_port(tool_id)
            )
        candidate_identity = (backend_port, shutdown_token)
        if candidate_identity in seen_candidates:
            continue
        seen_candidates.add(candidate_identity)
        owner_candidates.append(
            {
                "backend_port": backend_port,
                "shutdown_token": shutdown_token,
                "owner": owner,
                "owner_path": owner_path,
            }
        )

    for candidate in owner_candidates:
        backend_port = int(candidate["backend_port"])
        shutdown_token = str(candidate["shutdown_token"])
        health_unavailable = False
        try:
            with urllib_request.urlopen(
                f"http://127.0.0.1:{backend_port}/health",
                timeout=1.5,
            ) as response:
                health = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            urllib_error.URLError,
        ):
            health_unavailable = True
            health = None
        if health_unavailable:
            if _force_stop_verified_unresponsive_backend(
                tool_id=tool_id,
                owner=dict(candidate["owner"]),
                owner_path=Path(candidate["owner_path"]),
                backend_port=backend_port,
                live_app_dir=live_app_dir,
            ):
                return True
            continue
        if (
            not isinstance(health, dict)
            or str(health.get("workspace_instance_id") or "")
            != expected_instance
        ):
            continue

        shutdown_request = urllib_request.Request(
            f"http://127.0.0.1:{backend_port}/shutdown",
            headers={"X-GPTBridge-Shutdown-Token": shutdown_token},
        )
        try:
            with urllib_request.urlopen(shutdown_request, timeout=3) as response:
                if int(getattr(response, "status", 0)) != 200:
                    continue
        except (OSError, urllib_error.URLError):
            continue

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                urllib_request.urlopen(
                    f"http://127.0.0.1:{backend_port}/health",
                    timeout=0.3,
                ).close()
            except (OSError, urllib_error.URLError):
                return True
            time.sleep(0.2)
        raise RuntimeError(
            f"Packaged backend on port {backend_port} did not stop "
            "before the update timeout"
        )
    return False
