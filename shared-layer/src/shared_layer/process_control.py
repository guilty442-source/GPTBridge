"""Information-layer-owned adapter for governed CLI process control (A177).

A177 forbids domain or presentation code from opening a direct subprocess
control channel: cross-process communication must pass through an
information-layer-owned adapter that enforces an executable allowlist and
a bounded timeout, and records every invocation.  ``GovernedProcessAdapter``
is that adapter — callers may only drive the exact executables they
registered, and each run leaves an audit record on the adapter.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence


DEFAULT_TIMEOUT_SECONDS: Final[float] = 600.0
_AUDIT_TRAIL_LIMIT: Final[int] = 100


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GovernedProcessAdapter:
    """Allowlist + bounded timeout + audit for governed CLI process control."""

    def __init__(self, allowed_executables: Sequence[str | Path]) -> None:
        candidates = [
            str(Path(str(item)).resolve())
            for item in allowed_executables
            if str(item or "").strip()
        ]
        self._allowed = {os.path.normcase(item) for item in candidates}
        self._audit_trail: list[dict[str, Any]] = []

    @staticmethod
    def discover_python() -> str | None:
        """First usable Python interpreter on this host (adapter-owned)."""
        for candidate in (sys.executable, shutil.which("python.exe"), shutil.which("python")):
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    @property
    def audit_trail(self) -> list[dict[str, Any]]:
        return list(self._audit_trail)

    def run_json(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cwd: str | Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run an allowlisted command and return its JSON report.

        Fail-closed: empty argv, an executable outside the allowlist or a
        timeout/dll error never reaches the process table; every completed
        invocation is recorded on the adapter's audit trail.
        """
        if not argv:
            return {"ok": False, "error_code": "PROCESS_ARGV_REQUIRED"}
        executable = os.path.normcase(str(Path(str(argv[0])).resolve()))
        if executable not in self._allowed:
            return {
                "ok": False,
                "error_code": "PROCESS_EXECUTABLE_NOT_ALLOWED",
                "message": str(argv[0]),
            }
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [str(argument) for argument in argv],
                cwd=os.fspath(cwd) if cwd is not None else None,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            record = self._record(argv, None, started, "timeout")
            return {
                "ok": False,
                "error_code": "PROCESS_TIMEOUT",
                "message": f"governed process timed out after {timeout_seconds}s",
                "audit": record,
            }
        except OSError as error:
            record = self._record(argv, None, started, type(error).__name__)
            return {
                "ok": False,
                "error_code": "PROCESS_LAUNCH_FAILED",
                "message": str(error),
                "audit": record,
            }
        record = self._record(argv, completed.returncode, started, "completed")
        output = (completed.stdout or "").strip()
        report: dict[str, Any] = {}
        for line in reversed(output.splitlines()):
            candidate = line.strip()
            if not candidate.startswith("{"):
                continue
            try:
                report = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        if not report:
            return {
                "ok": False,
                "error_code": "PROCESS_OUTPUT_INVALID",
                "exit_code": completed.returncode,
                "stdout": output[:4000],
                "stderr": (completed.stderr or "")[:4000],
                "audit": record,
            }
        report["exit_code"] = completed.returncode
        if completed.returncode != 0:
            report["ok"] = False
        report.setdefault("audit", record)
        return report

    def _record(
        self,
        argv: Sequence[str],
        returncode: int | None,
        started: float,
        outcome: str,
    ) -> dict[str, Any]:
        record = {
            "argv": [str(argument) for argument in argv],
            "returncode": returncode,
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "at": _iso_now(),
        }
        self._audit_trail.append(record)
        overflow = len(self._audit_trail) - _AUDIT_TRAIL_LIMIT
        if overflow > 0:
            del self._audit_trail[:overflow]
        return record


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "GovernedProcessAdapter"]
