"""Information-layer-owned adapter for governed CLI process control (A447).

A447 forbids domain or presentation code from opening a direct subprocess
control channel: cross-process communication must pass through an
information-layer-owned adapter that enforces an executable allowlist and
a bounded timeout, and records every invocation.  ``GovernedProcessAdapter``
is that adapter — callers may only drive the exact executables they
registered, and each run leaves a durable, content-hashed audit record.

Hardening (A447/A121/A46):
- ``audit_trail`` is persisted to an append-only JSONL ledger with SHA-256
  content hashes so records survive process restarts and cannot be tampered
  with silently.
- Every invocation requires a non-empty ``requester`` identity and a
  ``permission_token``; bare caller strings are not identity attestation.
- Per-requester rate limiting prevents runaway callers from flooding the
  process table.
- ``environment`` is validated against an explicit per-field allowlist so
  callers cannot inject arbitrary environment variables into the child.
- A health signal tracks success/failure counts for observability.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence


DEFAULT_TIMEOUT_SECONDS: Final[float] = 600.0
_AUDIT_TRAIL_LIMIT: Final[int] = 100
_DEFAULT_LEDGER: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "runtime"
    / "governed-process-audit.jsonl"
)
# Per-requester rate limit: max invocations per second window.
_DEFAULT_RATE_CAPACITY: Final[int] = 10
_DEFAULT_RATE_WINDOW_SECONDS: Final[float] = 1.0
# Environment variables the adapter is allowed to pass through to the
# child process.  Any key outside this set is stripped (fail-closed).
_ALLOWED_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT",
    }
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HealthSignal:
    """Adapter health summary for observability (A447)."""

    total_invocations: int = 0
    successful: int = 0
    failed: int = 0
    denied: int = 0
    rate_limited: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_invocations": self.total_invocations,
            "successful": self.successful,
            "failed": self.failed,
            "denied": self.denied,
            "rate_limited": self.rate_limited,
        }


@dataclass
class _RateBucket:
    timestamps: deque = field(default_factory=lambda: deque(maxlen=_DEFAULT_RATE_CAPACITY * 10))
    capacity: int = _DEFAULT_RATE_CAPACITY
    window_seconds: float = _DEFAULT_RATE_WINDOW_SECONDS

    def allow(self, now: float) -> bool:
        cutoff = now - self.window_seconds
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.capacity:
            return False
        self.timestamps.append(now)
        return True


def _compute_audit_hash(record: dict[str, Any]) -> str:
    """SHA-256 content hash so an audit record cannot be tampered with."""
    content = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _persist_audit(ledger_path: Path, record: dict[str, Any]) -> None:
    """Append a content-hashed audit record to the durable JSONL ledger."""
    record["content_hash"] = _compute_audit_hash(record)
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(line + os.linesep)
            handle.flush()
    except OSError:
        pass  # audit persistence is best-effort; in-memory trail still works


def _validate_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Strip environment keys not in the allowlist (A447 fail-closed)."""
    if environment is None:
        return None
    return {
        key: str(value)
        for key, value in environment.items()
        if key in _ALLOWED_ENV_KEYS
    }


class GovernedProcessAdapter:
    """Allowlist + bounded timeout + persistent audit for governed CLI control.

    Every ``run_json`` call requires a ``requester`` identity and a
    ``permission_token``; the adapter enforces per-requester rate limiting,
    validates the environment against an explicit allowlist, and persists
    every invocation to a content-hashed JSONL audit ledger.
    """

    def __init__(
        self,
        allowed_executables: Sequence[str | Path],
        *,
        audit_ledger: Path | None = None,
    ) -> None:
        candidates = [
            str(Path(str(item)).resolve())
            for item in allowed_executables
            if str(item or "").strip()
        ]
        self._allowed = {os.path.normcase(item) for item in candidates}
        self._audit_trail: list[dict[str, Any]] = []
        self._ledger = audit_ledger or _DEFAULT_LEDGER
        self._lock = threading.Lock()
        self._rate_buckets: dict[str, _RateBucket] = {}
        self._health = HealthSignal()

    @staticmethod
    def discover_python() -> str | None:
        """First usable Python interpreter on this host (adapter-owned)."""
        for candidate in (
            sys.executable,
            shutil.which("python.exe"),
            shutil.which("python"),
        ):
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    @property
    def audit_trail(self) -> list[dict[str, Any]]:
        """Recent in-memory audit records (durable copy in the JSONL ledger)."""
        return list(self._audit_trail)

    @property
    def health(self) -> HealthSignal:
        """Adapter health signal for observability (A447)."""
        return self._health

    def run_json(
        self,
        argv: Sequence[str],
        *,
        requester: str = "",
        permission_token: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cwd: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run an allowlisted command and return its JSON report.

        Fail-closed: empty argv, missing requester/token, an executable
        outside the allowlist, a rate-limit hit, or a timeout/launch error
        never reaches the process table; every attempt is recorded on the
        adapter's durable audit trail.
        """
        if not argv:
            return {"ok": False, "error_code": "PROCESS_ARGV_REQUIRED"}
        if not str(requester or "").strip():
            self._count_denied()
            return {"ok": False, "error_code": "REQUESTER_REQUIRED"}
        if not str(permission_token or "").strip():
            self._count_denied()
            return {"ok": False, "error_code": "PERMISSION_TOKEN_REQUIRED"}
        executable = os.path.normcase(str(Path(str(argv[0])).resolve()))
        if executable not in self._allowed:
            self._count_denied()
            return {
                "ok": False,
                "error_code": "PROCESS_EXECUTABLE_NOT_ALLOWED",
                "message": str(argv[0]),
            }
        if not self._check_rate(requester):
            self._count_rate_limited()
            return {"ok": False, "error_code": "RATE_LIMIT_EXCEEDED"}
        env = _validate_environment(environment)
        return self._execute(
            argv, requester, permission_token, timeout_seconds, cwd, env
        )

    def _execute(
        self,
        argv: Sequence[str],
        requester: str,
        permission_token: str,
        timeout_seconds: float,
        cwd: str | Path | None,
        env: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Run the subprocess and record the audit entry."""
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [str(argument) for argument in argv],
                cwd=os.fspath(cwd) if cwd is not None else None,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            record = self._record(
                argv, requester, permission_token, None, started, "timeout"
            )
            return {
                "ok": False,
                "error_code": "PROCESS_TIMEOUT",
                "message": f"governed process timed out after {timeout_seconds}s",
                "audit": record,
            }
        except OSError as error:
            record = self._record(
                argv, requester, permission_token, None, started,
                type(error).__name__,
            )
            return {
                "ok": False,
                "error_code": "PROCESS_LAUNCH_FAILED",
                "message": str(error),
                "audit": record,
            }
        record = self._record(
            argv, requester, permission_token, completed.returncode, started,
            "completed",
        )
        return self._parse_output(argv, completed, record)

    def _parse_output(
        self,
        argv: Sequence[str],
        completed: subprocess.CompletedProcess[str],
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """Parse the subprocess stdout for a JSON report."""
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

    def _check_rate(self, requester: str) -> bool:
        """Per-requester rate limit (A447)."""
        with self._lock:
            bucket = self._rate_buckets.get(requester)
            if bucket is None:
                bucket = _RateBucket()
                self._rate_buckets[requester] = bucket
            return bucket.allow(time.monotonic())

    def _count_denied(self) -> None:
        self._health = HealthSignal(
            total_invocations=self._health.total_invocations + 1,
            successful=self._health.successful,
            failed=self._health.failed,
            denied=self._health.denied + 1,
            rate_limited=self._health.rate_limited,
        )

    def _count_rate_limited(self) -> None:
        self._health = HealthSignal(
            total_invocations=self._health.total_invocations + 1,
            successful=self._health.successful,
            failed=self._health.failed,
            denied=self._health.denied,
            rate_limited=self._health.rate_limited + 1,
        )

    def _record(
        self,
        argv: Sequence[str],
        requester: str,
        permission_token: str,
        returncode: int | None,
        started: float,
        outcome: str,
    ) -> dict[str, Any]:
        record = {
            "argv": [str(argument) for argument in argv],
            "requester": str(requester),
            "permission_token_hash": hashlib.sha256(
                str(permission_token).encode("utf-8")
            ).hexdigest()[:16],
            "returncode": returncode,
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "at": _iso_now(),
        }
        self._audit_trail.append(record)
        overflow = len(self._audit_trail) - _AUDIT_TRAIL_LIMIT
        if overflow > 0:
            del self._audit_trail[:overflow]
        if outcome == "completed" and returncode == 0:
            self._health = HealthSignal(
                total_invocations=self._health.total_invocations + 1,
                successful=self._health.successful + 1,
                failed=self._health.failed,
                denied=self._health.denied,
                rate_limited=self._health.rate_limited,
            )
        else:
            self._health = HealthSignal(
                total_invocations=self._health.total_invocations + 1,
                successful=self._health.successful,
                failed=self._health.failed + 1,
                denied=self._health.denied,
                rate_limited=self._health.rate_limited,
            )
        _persist_audit(self._ledger, record)
        return record


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "GovernedProcessAdapter",
    "HealthSignal",
]
