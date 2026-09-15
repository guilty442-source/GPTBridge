# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import stat as stat_module
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request

from .business_history import BusinessHistoryStore

try:
    from governance_rule.execution.git_tiers import audit_log, enforce
except Exception:
    audit_log = None
    enforce = None

from .cleanup_helpers import SECONDS_PER_DAY, LEGACY_QUARANTINE_ROOT_NAME, LEGACY_RECOVERY_ROOT_NAME, QUARANTINE_RELATIVE_PATH, RECOVERY_RELATIVE_PATH, CLEANER_RUNTIME_RELATIVE_PATH, DEFAULT_QUARANTINE_TTL_HOURS, DEFAULT_PLAN_TTL_MINUTES, MAX_REPORTED_SKIPS, MAX_HISTORY_RECORDS, PROGRESS_JSON_PREFIX, MANAGED_BACKUP_SCHEMA_VERSION, MANAGED_BACKUP_RETENTION_PER_OWNER, MANAGED_BACKUP_RELATIVE_ROOT, BACKUP_EXTRACT_RELATIVE_ROOT, SYSTEM_RESCUE_REQUIRED_PATHS, SYSTEM_RESCUE_REPAIR_ANOMALIES, PLAN_SCHEMA_VERSION, QUARANTINE_SCHEMA_VERSION, CORE_EXCLUDED_DIRECTORY_NAMES, CORE_PROTECTED_RELATIVE_PATHS, SOURCE_LIKE_SUFFIXES, _PROCESS_LOCKS_GUARD, _PROCESS_LOCKS, FALLBACK_RULES, ProgressCallback, _background_subprocess_kwargs, _deep_merge, _parse_iso


class CleanupRepairOpsMixin:

            def _repair_base_result(
                self,
                *,
                dry_run: bool,
                include_shared: bool,
                selected_anomalies: set[str] | None,
                normalized_selection: set[str] | None,
                candidates: list[dict[str, Any]],
                diagnostics: list[dict[str, Any]],
            ) -> dict[str, Any]:
                return {
    "ok": True,
    "dry_run": dry_run,
    "include_shared": include_shared,
    "selected_anomalies": (
        sorted(normalized_selection)
        if selected_anomalies is not None
        else None
    ),
    "anomaly_count": len(candidates) + len(diagnostics),
    "repairable_count": len(candidates),
    "repaired_count": 0,
    "repaired_bytes": 0,
    "permanently_deleted": 0,
    "disk_space_reclaimed_bytes": 0,
    "items": candidates,
    "diagnostics": diagnostics,
    "errors": [],
    "message": (
        f"anomaly scan completed (repairable={len(candidates)}, "
        f"diagnostic={len(diagnostics)})"
    ),
}



            def repair_anomalies(
                self,
                *,
                dry_run: bool = False,
                include_shared: bool = True,
                selected_anomalies: set[str] | None = None,
                now: float | None = None,
            ) -> dict[str, Any]:
                candidates, diagnostics = self._repair_anomaly_candidates(
                    include_shared=include_shared,
                    now=now,
                )
                normalized_selection: set[str] | None = None
                if selected_anomalies is not None:
                    normalized_selection = {
                        str(item).strip()
                        for item in selected_anomalies
                        if str(item).strip()
                    }
                    candidates = [
                        item
                        for item in candidates
                        if str(item.get("anomaly") or "") in normalized_selection
                    ]
                base_result = self._repair_base_result(
                    dry_run=dry_run,
                    include_shared=include_shared,
                    selected_anomalies=selected_anomalies,
                    normalized_selection=normalized_selection,
                    candidates=candidates,
                    diagnostics=diagnostics,
                )
                if dry_run or not candidates:
                    return base_result

                with self._mutation_guard("anomaly-repair") as lock:
                    if not lock.get("acquired"):
                        return {
                            **base_result,
                            "ok": False,
                            "busy": True,
                            "message": str(
                                lock.get("message") or "project cleaner is busy"
                            ),
                        }
                    return self._apply_anomaly_repair_locked(
                        base_result, candidates, include_shared
                    )


            def _finish_anomaly_repair(
                self,
                base_result: dict[str, Any],
                entries: list[dict[str, Any]],
                repaired_count: int,
                repaired_bytes: int,
                errors: list[dict[str, Any]],
                skipped: list[dict[str, Any]],
                batch_dir: Path,
                manifest_path: Path,
                batch_name: str,
                include_shared: bool,
            ) -> dict[str, Any]:
                result = {
                    **base_result,
                    "ok": not errors,
                    "repaired_count": repaired_count,
                    "repaired_bytes": repaired_bytes,
                    "quarantine": True,
                    "quarantine_path": str(batch_dir),
                    "quarantine_manifest": str(manifest_path),
                    "items": entries,
                    "skipped": skipped,
                    "errors": errors,
                    "message": (
                        "anomaly repair completed "
                        f"(repaired={repaired_count}, skipped={len(skipped)}, "
                        f"errors={len(errors)})"
                    ),
                }
                self._append_history(
                    "anomaly-repair",
                    ok=result["ok"],
                    scope="global" if include_shared else "runtime",
                    item_count=repaired_count,
                    bytes=repaired_bytes,
                    skipped=len(skipped),
                    errors=len(errors),
                    batch_id=batch_name,
                )
                self._emit_progress(
                    "repair",
                    100,
                    "Recoverable anomaly repair completed",
                    repaired_count=repaired_count,
                )
                return result







            def _system_rescue_subprocess(
                self,
                command: list[str],
                *,
                timeout_seconds: int,
            ) -> dict[str, Any]:
                environment = os.environ.copy()
                environment.pop("ELECTRON_RUN_AS_NODE", None)
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment["PYTHONNOUSERSITE"] = "1"
                try:
                    completed = subprocess.run(
                        command,
                        cwd=str(self.project_root),
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout_seconds,
                        check=False,
                        **_background_subprocess_kwargs(),
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    return {
                        "ok": False,
                        "exit_code": 124
                        if isinstance(exc, subprocess.TimeoutExpired)
                        else -1,
                        "output": str(exc),
                    }
                return {
                    "ok": completed.returncode == 0,
                    "exit_code": completed.returncode,
                    "output": completed.stdout[-12000:],
                }

            def _system_rescue_package_check(self) -> dict[str, Any]:
                script = (
                    self.project_root
                    / "system-rescue"
                    / "src"
                    / "backend"
                    / "services"
                    / "system_rescue"
                    / "integration"
                    / "platform_packager.py"
                )
                if (
                    not script.is_file()
                    or self._is_link_or_reparse_point(script)
                    or not self._inside_project(script)
                ):
                    return {
                        "ok": False,
                        "available": False,
                        "message": "package verification entry is unavailable",
                        "results": [],
                    }
                run = self._run_package_verification(script)
                return self._package_check_result(run)

            def _run_package_verification(self, script: Path) -> dict[str, Any]:
                run = self._system_rescue_subprocess(
                    [
                        sys.executable,
                        "-B",
                        "-s",
                        str(script),
                        "--all",
                        "--verify",
                        "--json",
                    ],
                    timeout_seconds=180,
                )
                payload: dict[str, Any] = {}
                for line in reversed(str(run.get("output") or "").splitlines()):
                    try:
                        loaded = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(loaded, dict):
                        payload = loaded
                        break
                run["payload"] = payload
                return run

            @staticmethod
            def _package_check_result(run: dict[str, Any]) -> dict[str, Any]:
                payload = run.get("payload") or {}
                results = payload.get("results", [])
                if not isinstance(results, list):
                    results = []
                package_ok = bool(run.get("ok")) and payload.get("ok") is True
                deferred_results = [
                    item
                    for item in results
                    if isinstance(item, dict)
                    and (
                        item.get("error_code") == "STALE_PACKAGE"
                        or (
                            item.get("tool_id") == "governance_rule"
                            and item.get("error_code") == "PACKAGE_MISSING"
                        )
                    )
                ]
                blocking_results = [
                    item for item in results if item not in deferred_results
                ]
                packaging_deferred = bool(deferred_results) and not blocking_results
                return {
                    "ok": package_ok or packaging_deferred,
                    "available": True,
                    "exit_code": run.get("exit_code"),
                    "results": results,
                    "blocking_results": blocking_results,
                    "deferred_results": deferred_results,
                    "packaging_deferred": packaging_deferred,
                    "message": (
                        "all packaged tools are current"
                        if package_ok
                        else (
                            "existing packages are structurally usable; source freshness "
                            "is deferred until packaging is explicitly requested"
                            if packaging_deferred
                            else "one or more packaged tools failed integrity verification"
                        )
                    ),
                    "output": str(run.get("output") or "")[-4000:],
                }
