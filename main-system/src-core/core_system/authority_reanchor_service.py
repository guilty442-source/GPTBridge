"""Governed authority re-anchor service — authority updates without restarts.

A codex or managed-registry update may happen at any time while the backend
is running (parallel, multi-writer).  Previously a changed authority file
invalidated the launch integrity manifest and readiness stayed false until
a process restart.  This service closes that gap:

  1. polls the protected authority files by content hash;
  2. requires the digest snapshot to be stable across two reads so a
     mid-write update is never adopted (parallel-update safe);
  3. validates the new authority: the governance audit subprocess must PASS
     and the codex must load at the current authority version;
  4. re-certifies the permission sovereign (codex reload + version refresh);
  5. re-anchors the launch integrity manifest in-process;
  6. verifies readiness and writes an audit record.

Failures keep the previous baseline and are retried with backoff; the
service runs on its own thread, never on the IPC event loop, and never
crashes the backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from governance_rule.execution.codex_reconcile import bounded_lookup
from governance_rule.execution.versioning import validate_loaded_authority_version
from governance_rule.governance_policy import governance_policy_snapshot
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.execution.path_guard import (
    resolve_project_path,
)

AUTHORITY_REANCHOR_VERSION: Final[str] = "1.0.0"
POLL_INTERVAL_SECONDS: Final[float] = 3.0
STABILITY_DELAY_SECONDS: Final[float] = 2.0
AUDIT_TIMEOUT_SECONDS: Final[float] = 180.0
FAILURE_BACKOFF_SECONDS: Final[float] = 5.0
STATE_RELATIVE: Final[tuple[str, ...]] = (
    "main-system",
    "runtime",
    "state",
    "authority-reanchor.json",
)
AUDIT_RELATIVE: Final[tuple[str, ...]] = (
    "main-system",
    "runtime",
    "state",
    "authority-reanchor-audit.jsonl",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def authority_file_digest(paths: list[Path]) -> str:
    """Combined content digest of the authority files, in caller order."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


class AuthorityReanchorService:
    """Watches protected authority files and re-anchors the running process."""

    VERSION = AUTHORITY_REANCHOR_VERSION

    def __init__(self, app: Any) -> None:
        self.app = app
        raw_root = getattr(app, "project_root", "") or "."
        self._project_root = Path(raw_root).resolve()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._baseline = ""
        self._backoff_until = 0.0
        self._status: dict[str, Any] = {
            "state": "starting",
            "anchored_at": "",
            "attempts": 0,
            "last_error": "",
        }

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            digest, _paths = self._snapshot()
            self._baseline = digest
            self._status.update(state="anchored", anchored_at=_iso_now())
        except Exception as error:  # never block startup
            self._status.update(
                state="error", last_error=f"{type(error).__name__}: {error}"
            )
        self._write_state()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="authority-reanchor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": AUTHORITY_REANCHOR_VERSION,
                **self._status,
                "baseline_digest": self._baseline[:16],
            }

    # ── observation ───────────────────────────────────────────────────

    def _authority_paths(self) -> list[Path]:
        policy = governance_policy_snapshot()
        directory = directory_authority_snapshot()
        relatives = (
            *policy.authority_files,
            *directory.managed_read_only_registry_paths,
        )
        return [resolve_project_path(self._project_root, relative) for relative in relatives]

    def _snapshot(self) -> tuple[str, list[Path]]:
        paths = self._authority_paths()
        return authority_file_digest(paths), paths

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._probe_once()
            except Exception as error:  # best-effort; never crash
                self._status.update(
                    last_error=f"{type(error).__name__}: {error}"
                )
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _probe_once(self) -> None:
        now = time.monotonic()
        if now < self._backoff_until:
            return
        digest, _paths = self._snapshot()
        if digest == self._baseline:
            return
        # Require a stable snapshot: a parallel writer may be mid-update.
        self._stop.wait(STABILITY_DELAY_SECONDS)
        stable_digest, stable_paths = self._snapshot()
        if stable_digest != digest:
            return
        self._adopt(stable_digest, stable_paths)

    # ── adoption ──────────────────────────────────────────────────────

    def _adopt(self, digest: str, paths: list[Path]) -> None:
        with self._lock:
            self._status.update(
                state="validating",
                attempts=int(self._status.get("attempts", 0)) + 1,
            )

            def defer(reason: str) -> None:
                self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
                self._status.update(state="deferred", last_error=reason)
                self._write_state()
                self._audit({"event": "deferred", "reason": reason})

            if not self._run_audit():
                defer("governance audit failed")
                return
            try:
                # A435 bounded lookup: codex identity only — verifies the
                # authority loads at the current version through the
                # official entry rather than a direct repository read.
                bounded_lookup(
                    "authority-reanchor-service",
                    purpose="status",
                    scope=("codex:identity",),
                    reader=lambda ctx: ctx.codex_identity(),
                )
                validate_loaded_authority_version()
            except Exception as error:
                defer(f"codex-invalid: {type(error).__name__}: {error}")
                return
            try:
                from governance.sovereigns.permission_sovereign import (
                    re_certify_permission_sovereign,
                )

                re_certify_permission_sovereign()
            except Exception as error:
                defer(
                    f"permission-recertify-failed: {type(error).__name__}: {error}"
                )
                return

            governance = getattr(self.app, "governance", None)
            reanchor = getattr(governance, "reanchor_runtime_integrity", None)
            if not callable(reanchor):
                defer("governance-runtime-unavailable")
                return
            try:
                reanchor()
            except Exception as error:
                defer(f"reanchor-failed: {type(error).__name__}: {error}")
                return
            ready = getattr(governance, "runtime_integrity_ready", None)
            try:
                if callable(ready) and not ready(max_age_seconds=0):
                    defer("post-reanchor-integrity-check-failed")
                    return
            except Exception as error:
                defer(f"post-check-error: {type(error).__name__}: {error}")
                return

            self._baseline = digest
            self._backoff_until = 0.0
            self._status.update(
                state="anchored",
                anchored_at=_iso_now(),
                last_error="",
            )
            self._write_state()
            self._audit(
                {
                    "event": "anchored",
                    "file_count": len(paths),
                    "digest": digest[:16],
                }
            )

    def _run_audit(self) -> bool:
        """Run the same governance audit gate the boot core uses."""
        command = [sys.executable, "-m", "governance_rule.execution.audit"]
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
        try:
            completed = subprocess.run(
                command,
                cwd=str(self._project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=AUDIT_TIMEOUT_SECONDS,
                creationflags=creationflags,
            )
            return completed.returncode == 0
        except Exception:
            return False

    # ── observability ─────────────────────────────────────────────────

    def _write_state(self) -> None:
        try:
            path = self._project_root.joinpath(*STATE_RELATIVE)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": AUTHORITY_REANCHOR_VERSION,
                **_status_snapshot(self._status),
                "baseline_digest": self._baseline,
                "updated_at": _iso_now(),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass

    def _audit(self, entry: dict[str, Any]) -> None:
        try:
            path = self._project_root.joinpath(*AUDIT_RELATIVE)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"timestamp": _iso_now(), **entry},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
        except OSError:
            pass


def _status_snapshot(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": str(status.get("state", "")),
        "anchored_at": str(status.get("anchored_at", "")),
        "attempts": int(status.get("attempts", 0)),
        "last_error": str(status.get("last_error", "")),
    }


__all__ = [
    "AUTHORITY_REANCHOR_VERSION",
    "AuthorityReanchorService",
    "authority_file_digest",
]
