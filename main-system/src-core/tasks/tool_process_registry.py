from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


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


def _powershell_process_ids(command: str, environment: dict[str, str]) -> list[int]:
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
            env=environment,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    process_ids: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            process_ids.append(int(line.strip()))
        except ValueError:
            continue
    return process_ids


# ------------------------------------------------------------------
# Batched process snapshot (single PowerShell call for all tools)
# ------------------------------------------------------------------

_BATCH_PROCESS_COMMAND = (
    "Get-CimInstance Win32_Process | "
    "Where-Object { "
    "$_.Name -in @('python.exe','pythonw.exe','electron.exe') "
    "-and ($_.CommandLine -or $_.ExecutablePath) "
    "} | Select-Object ProcessId,Name,CommandLine,ExecutablePath | "
    "ConvertTo-Json -Compress -Depth 2"
)


def _snapshot_processes() -> list[dict[str, Any]]:
    """Return all python/pythonw/electron processes in a single PowerShell call."""
    if os.name != "nt":
        return []
    try:
        completed = _run_hidden_subprocess(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _BATCH_PROCESS_COMMAND,
            ],
            timeout=10,
        )
    except Exception:
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    raw = completed.stdout.strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    result: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "pid": item.get("ProcessId", 0),
                "name": str(item.get("Name", "")),
                "command_line": str(item.get("CommandLine", "") or ""),
                "executable_path": str(item.get("ExecutablePath", "") or ""),
            }
        )
    return result


def _match_process_to_tool(
    proc: dict[str, Any],
    tool_id: str,
    source_runtime_entry: str,
    executable_path: str,
) -> tuple[bool, bool, bool]:
    """Return (is_source_runtime, is_executable, is_source_ui) for one process."""
    name = proc["name"].lower()
    cmd = proc["command_line"]
    exe = proc["executable_path"]
    pid = proc["pid"]
    if not isinstance(pid, int) or pid <= 0:
        return (False, False, False)
    is_runtime = bool(
        source_runtime_entry
        and name in ("python.exe", "pythonw.exe")
        and cmd
        and source_runtime_entry.lower() in cmd.lower()
    )
    is_executable = bool(
        executable_path
        and exe
        and os.path.normcase(exe) == os.path.normcase(executable_path)
    )
    is_ui = bool(
        name == "electron.exe"
        and cmd
        and "source-tool-ui-host\\main.cjs".lower() in cmd.lower()
        and f"--tool-id={tool_id}".lower() in cmd.lower()
    )
    return (is_runtime, is_executable, is_ui)


def batch_running_status(
    tools: list[dict[str, Any]],
) -> dict[str, dict[str, list[int]]]:
    """Check running status for all tools in a single PowerShell call.

    Returns ``{tool_id: {"source_runtime": [...], "executable": [...],
    "source_ui": [...]}}``.
    """
    if os.name != "nt":
        return {}
    snapshot = _snapshot_processes()
    if not snapshot:
        return {}

    result: dict[str, dict[str, list[int]]] = {}
    for tool in tools:
        tool_id = str(tool.get("id", "")).strip()
        if not tool_id:
            continue
        entry: dict[str, list[int]] = {
            "source_runtime": [],
            "executable": [],
            "source_ui": [],
        }
        source_runtime_entry = str(tool.get("source_runtime_entry", "")).strip()
        executable_path = str(tool.get("executable_path", "")).strip()
        for proc in snapshot:
            is_rt, is_exe, is_ui = _match_process_to_tool(
                proc, tool_id, source_runtime_entry, executable_path
            )
            pid = proc["pid"]
            if is_rt:
                entry["source_runtime"].append(pid)
            if is_exe:
                entry["executable"].append(pid)
            if is_ui:
                entry["source_ui"].append(pid)
        result[tool_id] = entry
    return result


def running_executable_process_ids(executable_file: Path) -> list[int]:
    if os.name != "nt":
        return []
    environment = os.environ.copy()
    environment["GPTBRIDGE_EXECUTABLE_PATH"] = str(executable_file.resolve())
    command = (
        "$target = [System.IO.Path]::GetFullPath($env:GPTBRIDGE_EXECUTABLE_PATH);"
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -and "
        "([System.IO.Path]::GetFullPath($_.ExecutablePath)).Equals("
        "$target, [System.StringComparison]::OrdinalIgnoreCase) } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    return _powershell_process_ids(command, environment)


def running_source_runtime_process_ids(entry_file: Path) -> list[int]:
    if os.name != "nt":
        return []
    environment = os.environ.copy()
    environment["GPTBRIDGE_SOURCE_RUNTIME_ENTRY"] = str(entry_file.resolve())
    command = (
        "$target=[System.IO.Path]::GetFullPath($env:GPTBRIDGE_SOURCE_RUNTIME_ENTRY);"
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and "
        "$_.CommandLine.IndexOf($target,[System.StringComparison]::OrdinalIgnoreCase) -ge 0 "
        "} | Select-Object -ExpandProperty ProcessId"
    )
    return _powershell_process_ids(command, environment)


def running_packaged_backend_process_ids(tool_dir: Path) -> list[int]:
    if os.name != "nt":
        return []
    environment = os.environ.copy()
    environment["GPTBRIDGE_PACKAGED_TOOL_ROOT"] = str(tool_dir.resolve())
    command = (
        "$root=[System.IO.Path]::GetFullPath($env:GPTBRIDGE_PACKAGED_TOOL_ROOT);"
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('python.exe','pythonw.exe') -and $_.ExecutablePath -and "
        "$_.CommandLine -and "
        "$_.ExecutablePath.IndexOf($root,[System.StringComparison]::OrdinalIgnoreCase) -eq 0 -and "
        "$_.CommandLine.IndexOf('channel_runtime.py',[System.StringComparison]::OrdinalIgnoreCase) -ge 0 "
        "} | Select-Object -ExpandProperty ProcessId"
    )
    return _powershell_process_ids(command, environment)


def running_source_ui_process_ids(tool_id: str) -> list[int]:
    if os.name != "nt" or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", tool_id) is None:
        return []
    environment = os.environ.copy()
    environment["GPTBRIDGE_SOURCE_UI_TOOL_ID_QUERY"] = tool_id
    command = (
        "$toolId=$env:GPTBRIDGE_SOURCE_UI_TOOL_ID_QUERY;"
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -eq 'electron.exe' -and $_.CommandLine -and "
        "$_.CommandLine.IndexOf('source-tool-ui-host\\main.cjs',"
        "[System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and "
        "$_.CommandLine.IndexOf(('--tool-id=' + $toolId),"
        "[System.StringComparison]::OrdinalIgnoreCase) -ge 0 "
        "} | Select-Object -ExpandProperty ProcessId"
    )
    return _powershell_process_ids(command, environment)


def _force_stop_process_ids(process_ids: list[int]) -> list[int]:
    if os.name != "nt" or not process_ids:
        return []
    environment = os.environ.copy()
    environment["GPTBRIDGE_PROCESS_IDS"] = ",".join(str(pid) for pid in process_ids)
    command = (
        "$ids=$env:GPTBRIDGE_PROCESS_IDS -split ',' | Where-Object { $_ } | "
        "ForEach-Object { [int]$_ };"
        "foreach ($id in $ids) { Stop-Process -Id $id -Force "
        "-ErrorAction SilentlyContinue };$ids"
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
            env=environment,
        )
    except Exception:
        return []
    return process_ids


def stop_running_source_ui(tool_id: str) -> list[int]:
    process_ids = running_source_ui_process_ids(tool_id)
    if os.name != "nt" or not process_ids:
        return []
    for process_id in process_ids:
        try:
            _run_hidden_subprocess(
                ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
                timeout=8,
            )
        except Exception:
            continue
    return process_ids


def stop_running_executable(executable_file: Path) -> list[int]:
    return _force_stop_process_ids(running_executable_process_ids(executable_file))


def stop_running_source_runtime(entry_file: Path) -> list[int]:
    return _force_stop_process_ids(running_source_runtime_process_ids(entry_file))


def stop_running_packaged_backend(tool_dir: Path) -> list[int]:
    return _force_stop_process_ids(running_packaged_backend_process_ids(tool_dir))


__all__ = (
    "batch_running_status",
    "running_executable_process_ids",
    "running_packaged_backend_process_ids",
    "running_source_runtime_process_ids",
    "running_source_ui_process_ids",
    "stop_running_executable",
    "stop_running_packaged_backend",
    "stop_running_source_runtime",
    "stop_running_source_ui",
)
