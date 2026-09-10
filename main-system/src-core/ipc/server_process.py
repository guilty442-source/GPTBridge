"""Port-owner detection and stale-process inspection helpers (Windows).

Extracted from ``ipc.server`` to keep each module focused and under 500 lines.
All names here are re-exported by ``ipc.server`` for backward compatibility.
"""

# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

from .server_tokens import _background_subprocess_kwargs


def _get_port_owner(port: int) -> tuple[int | None, str | None]:
    if socket is None or sys.platform != "win32":
        return None, None
    try:
        output = subprocess.check_output(
            ["netstat", "-ano"],
            text=True,
            encoding="utf-8",
            errors="ignore",
            **_background_subprocess_kwargs(),
        )
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                pid = parts[-1]
                proc = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    **_background_subprocess_kwargs(),
                ).strip()
                try:
                    return int(pid), f"PID {pid} ({proc})"
                except ValueError:
                    return None, f"PID {pid} ({proc})"
    except Exception:
        return None, None
    return None, None


def _query_process_commandline(pid: int) -> tuple[str | None, str | None]:
    if sys.platform != "win32":
        return None, None
    try:
        output = subprocess.check_output(
            [
                "wmic",
                "process",
                "where",
                f"ProcessId={pid}",
                "get",
                "CommandLine,ExecutablePath",
                "/FORMAT:LIST",
            ],
            text=True,
            encoding="utf-8",
            errors="ignore",
            **_background_subprocess_kwargs(),
        )
        cmdline = None
        exe_path = None
        for line in output.splitlines():
            if line.startswith("CommandLine="):
                cmdline = line.partition("=")[2].strip()
            elif line.startswith("ExecutablePath="):
                exe_path = line.partition("=")[2].strip()
        return cmdline, exe_path
    except (OSError, subprocess.CalledProcessError):
        # Fallback for systems where wmic is unavailable (deprecated on Windows 11).
        pass
    try:
        # Use a single CIM query and emit CSV so the output is machine-parseable
        # without relying on $-variable interpolation inside the command string.
        ps = (
            f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
            "| Select-Object CommandLine, ExecutablePath -First 1 "
            "| ConvertTo-Csv -NoTypeInformation"
        )
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            encoding="utf-8",
            errors="ignore",
            **_background_subprocess_kwargs(),
        )
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if len(lines) >= 2 and lines[0].startswith('"CommandLine"'):
            import csv
            reader = csv.reader(lines[1:])
            row = next(reader, None)
            if row and len(row) >= 2:
                return row[0].strip() or None, row[1].strip() or None
    except Exception:
        pass
    return None, None


def _tasklist_image_name(pid: int) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            encoding="utf-8",
            errors="ignore",
            **_background_subprocess_kwargs(),
        )
        parts = [p.strip().strip('"') for p in output.splitlines()[0].split(",")]
        if parts:
            return parts[0]
    except Exception:
        return None
    return None


def _is_gptbridge_process(pid: int, project_root: Path) -> bool:
    if sys.platform != "win32":
        return False
    if pid == os.getpid():
        return False
    cmdline, exe_path = _query_process_commandline(pid)
    if cmdline:
        normalized = cmdline.lower()
        project_path_lower = str(project_root).lower()
        if project_path_lower in normalized:
            return True
        if "run.py" in normalized and "--serve" in normalized:
            return True
        if "src-core\\main.py" in normalized or "src-core/main.py" in normalized:
            return True
        if "boot_core.py" in normalized:
            return True
        if "gptbridge" in normalized and project_path_lower in normalized:
            return True
    if exe_path:
        normalized_exe = exe_path.lower()
        if str(project_root).lower() in normalized_exe:
            return True
        # A pythonw/python process from the same venv as this process is almost
        # certainly a stale GPTBridge backend when it is the one holding the IPC
        # port.  This covers the common case where pythonw has no visible
        # command-line string on Windows.
        this_exe = Path(sys.executable).resolve()
        that_exe = Path(exe_path).resolve()
        if (
            that_exe.name.lower().startswith("python")
            and that_exe.parent == this_exe.parent
        ):
            return True
    # Last-resort fallback for pythonw with an empty command line and a different
    # interpreter installation: any python process holding the dedicated IPC port
    # is the stale backend we are asked to replace.
    image = _tasklist_image_name(pid)
    if image and image.lower().startswith("python"):
        return True
    return False


def _kill_process(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
            **_background_subprocess_kwargs(),
        )
        return completed.returncode == 0
    except Exception:
        return False
