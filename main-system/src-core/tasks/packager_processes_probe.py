"""Packager processes — process probe helpers.

Extracted from packager_processes.py: running_executable_process_ids,
windows_process_owns_listening_port, restart_packaged_executable,
stop_running_executable_for_upgrade.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from packager_base import _background_subprocess_kwargs


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
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
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
