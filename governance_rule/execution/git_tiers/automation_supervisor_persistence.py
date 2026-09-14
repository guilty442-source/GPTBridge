"""Cross-reboot persistence for the automation supervisor.

Registers the supervisor as a Windows Task Scheduler logon job or a
per-user ``Run`` key so it survives reboots.  Both launch the
``scripts/git-supervisor.py`` wrapper (CWD-independent; ``-m`` cannot
resolve the package from an unrelated working directory).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

TASK_NAME: Final[str] = "GPTBridge-GitAutomation"
LOGO_NAME: Final[str] = "GPTBridge-GitAutomation"
LOGO_HIVE: Final[str] = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _supervisor_launch_arguments(root: Path) -> str:
    """CWD-independent supervisor launch (wrapper script, not ``-m``)."""
    wrapper = root / "scripts" / "git-supervisor.py"
    return (
        f'"{wrapper}" --root "{root}" --foreground --sync-interval 60 '
        f'--health-interval 20 --watch-interval 30 --debounce 60'
    )


def _installed_pythonw(root: Path) -> str:
    target = root / "main-system" / ".venv" / "Scripts" / "pythonw.exe"
    if target.is_file():
        return str(target)
    return str(sys.executable)


def _task_xml(arguments: str, target: str, root: Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="InteractiveUser">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="InteractiveUser">
    <Exec>
      <Command>{target}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def _install_task(root: Path) -> int:
    """Register a logon Task Scheduler job that keeps the supervisor alive."""
    import tempfile

    target = (
        str(root / "main-system" / ".venv" / "Scripts" / "pythonw.exe")
        if (root / "main-system" / ".venv" / "Scripts" / "pythonw.exe").is_file()
        else sys.executable
    )
    arguments = _supervisor_launch_arguments(root)
    xml = _task_xml(arguments, target, root)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", delete=False, encoding="utf-16"
    ) as handle:
        handle.write(xml)
        task_xml = handle.name
    try:
        result = subprocess.run(  # noqa: S603
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", task_xml, "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        try:
            os.unlink(task_xml)
        except OSError:
            pass
    if result.returncode != 0:
        print(f"task registration failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"task registered: {TASK_NAME}")
    return 0


def _uninstall_task() -> int:
    result = subprocess.run(  # noqa: S603
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 and "does not exist" not in result.stderr:
        print(f"task removal failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"task removed: {TASK_NAME}")
    return 0


def _install_logon(root: Path) -> int:
    """Register a per-user logon ``Run`` value for cross-reboot persistence."""
    import winreg

    pythonw = _installed_pythonw(root)
    arguments = _supervisor_launch_arguments(root)
    value = f'"{pythonw}" {arguments}'
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, LOGO_HIVE, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, LOGO_NAME, 0, winreg.REG_SZ, value)
    except OSError as exc:
        print(f"logon registration failed: {exc}", file=sys.stderr)
        return 1
    print(f"logon registration active: {LOGO_NAME}")
    return 0


def _uninstall_logon() -> int:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, LOGO_HIVE, 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                winreg.DeleteValue(key, LOGO_NAME)
            except FileNotFoundError:
                pass
    except OSError as exc:
        print(f"logon removal failed: {exc}", file=sys.stderr)
        return 1
    print(f"logon registration removed: {LOGO_NAME}")
    return 0
