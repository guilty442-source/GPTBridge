from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


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
    "running_executable_process_ids",
    "running_packaged_backend_process_ids",
    "running_source_runtime_process_ids",
    "running_source_ui_process_ids",
    "stop_running_executable",
    "stop_running_packaged_backend",
    "stop_running_source_runtime",
    "stop_running_source_ui",
)
