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


class CleanupRepairHealthMixin:

            @staticmethod
            def _system_rescue_main_health() -> dict[str, Any]:
                try:
                    with urllib_request.urlopen(
                        "http://127.0.0.1:8765/health",
                        timeout=2,
                    ) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                except (
                    OSError,
                    TimeoutError,
                    UnicodeError,
                    json.JSONDecodeError,
                    urllib_error.URLError,
                ) as exc:
                    return {
                        "ok": False,
                        "reachable": False,
                        "message": f"main backend is unavailable: {exc}",
                    }
                return {
                    "ok": payload.get("ok") is True,
                    "reachable": True,
                    "runtime_state": payload.get("runtime_state"),
                    "phase": payload.get("phase"),
                    "workspace_instance_id": payload.get("workspace_instance_id"),
                    "message": "main backend health endpoint responded",
                }

            def system_health_check(self, *, deep: bool = False) -> dict[str, Any]:
                """Run global read-only system diagnostics owned by Global Cleaner."""

                self._emit_progress("rescue", 5, "Checking project boundary")
                required: list[dict[str, Any]] = []
                for relative_path in SYSTEM_RESCUE_REQUIRED_PATHS:
                    target = self.project_root / relative_path
                    valid = False
                    reason = ""
                    try:
                        valid = (
                            self._inside_project(target)
                            and target.is_file()
                            and not self._is_link_or_reparse_point(target)
                        )
                        if not valid:
                            reason = "missing, non-regular, or outside project boundary"
                    except OSError as exc:
                        reason = str(exc)
                    required.append(
                        {
                            "path": relative_path,
                            "ok": valid,
                            "reason": reason,
                        }
                    )

                self._emit_progress("rescue", 25, "Validating system configuration")
                configurations: list[dict[str, Any]] = []
                for relative_path in (
                    "main-system/package.json",
                    "main-system/config/tool-runtime-contract.json",
                ):
                    target = self.project_root / relative_path
                    valid = False
                    reason = ""
                    try:
                        loaded = json.loads(target.read_text(encoding="utf-8"))
                        valid = isinstance(loaded, dict)
                        if not valid:
                            reason = "configuration root must be an object"
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        reason = str(exc)
                    configurations.append(
                        {
                            "path": relative_path,
                            "ok": valid,
                            "reason": reason,
                        }
                    )

                self._emit_progress("rescue", 45, "Checking package integrity")
                packages = self._system_rescue_package_check()
                anomalies = self.repair_anomalies(
                    dry_run=True,
                    include_shared=True,
                    selected_anomalies=set(SYSTEM_RESCUE_REPAIR_ANOMALIES),
                )
                environment = {
                    "python": {
                        "ok": Path(sys.executable).is_file(),
                        "path": sys.executable,
                    },
                    "node": {
                        "ok": shutil.which("node") is not None,
                        "path": shutil.which("node") or "",
                    },
                    "npm": {
                        "ok": shutil.which("npm.cmd" if os.name == "nt" else "npm")
                        is not None,
                        "path": shutil.which(
                            "npm.cmd" if os.name == "nt" else "npm"
                        )
                        or "",
                    },
                }
                main_health = self._system_rescue_main_health()
                typecheck: dict[str, Any] = {
                    "ok": True,
                    "skipped": True,
                    "message": "deep type check was not requested",
                }
                if deep:
                    self._emit_progress("rescue", 70, "Running deep type check")
                    npm = "npm.cmd" if os.name == "nt" else "npm"
                    typecheck = self._system_rescue_subprocess(
                        [
                            npm,
                            "--prefix",
                            str(self.project_root / "main-system"),
                            "run",
                            "type-check",
                        ],
                        timeout_seconds=180,
                    )
                    typecheck["skipped"] = False
                    typecheck["message"] = (
                        "type check passed"
                        if typecheck.get("ok")
                        else "type check failed"
                    )

                blocking: list[str] = []
                blocking.extend(
                    f"required:{item['path']}"
                    for item in required
                    if not item["ok"]
                )
                blocking.extend(
                    f"config:{item['path']}"
                    for item in configurations
                    if not item["ok"]
                )
                if not packages.get("ok"):
                    blocking.append("package-integrity")
                if not typecheck.get("ok"):
                    blocking.append("type-check")
                repairable_count = int(anomalies.get("repairable_count") or 0)
                state = (
                    "blocked"
                    if blocking
                    else "repairable"
                    if repairable_count
                    else "healthy"
                )
                result = {
                    "ok": not blocking,
                    "operation": "system-health-check",
                    "state": state,
                    "deep": deep,
                    "project_root": str(self.project_root),
                    "authority": "global-cleaner",
                    "boundary": "project-only",
                    "required_paths": required,
                    "configurations": configurations,
                    "environment": environment,
                    "packages": packages,
                    "main_health": main_health,
                    "typecheck": typecheck,
                    "anomalies": anomalies,
                    "repairable_count": repairable_count,
                    "blocking": blocking,
                    "message": (
                        "system rescue check passed"
                        if state == "healthy"
                        else "system rescue found recoverable anomalies"
                        if state == "repairable"
                        else "system rescue found blocking problems"
                    ),
                }
                self._append_history(
                    "system-health-check",
                    ok=result["ok"],
                    scope="global",
                    item_count=repairable_count,
                    errors=len(blocking),
                )
                self._emit_progress(
                    "rescue",
                    100,
                    "System rescue check completed",
                    state=state,
                )
                return result

            def get_status(self) -> dict[str, Any]:
                quarantine = self.list_quarantine_batches()
                repair_items, repair_diagnostics = self._repair_anomaly_candidates(
                    include_shared=True,
                )
                disk = shutil.disk_usage(self.project_root)
                return {
                    "ok": True,
                    "version": self.VERSION,
                    "project_root": str(self.project_root),
                    "supported_scopes": ["global", "runtime", "sandbox"],
                    "default_scope": "runtime",
                    "quarantine_root": str(self.quarantine_root),
                    "recovery_root": str(self.recovery_root),
                    "business_storage": {
                        "authority": "global-cleaner",
                        "database": str(self.business_history.database_path),
                    },
                    "managed_backups": self.list_managed_backups(),
                    "quarantine": quarantine,
                    "quarantine_health": quarantine.get("health", {}),
                    "history": list(reversed(self._history_records(limit=50))),
                    "rules": {
                        "schema_version": self.rules.get("schema_version"),
                        "override_path": str(self.rules_override_path),
                        "override_exists": self.rules_override_path.exists(),
                        "directory_rule_count": len(self.rules.get("directory_rules", [])),
                        "file_rule_count": len(self.rules.get("file_rules", [])),
                        "warnings": self.rule_warnings,
                    },
                    "anomalies": {
                        "repairable_count": len(repair_items),
                        "diagnostic_count": len(repair_diagnostics),
                        "items": repair_items,
                        "diagnostics": repair_diagnostics,
                    },
                    "disk": {
                        "total_bytes": disk.total,
                        "used_bytes": disk.used,
                        "free_bytes": disk.free,
                        "free_percent": round(disk.free / max(1, disk.total) * 100, 2),
                    },
                    "safety": {
                        "preview_plan_required": True,
                        "plan_signature": "HMAC-SHA256",
                        "git_protection": True,
                        "never_force_unlock": True,
                        "transactional_quarantine": True,
                        "permanent_delete": False,
                        "sha256_recovery_manifests": True,
                        "tombstones": True,
                    },
                    "capabilities": {
                        "mutation_root": str(self.project_root),
                        "read_only": [
                            "status",
                            "preview",
                            "storage-analysis",
                            "anomaly-diagnosis",
                        ],
                        "recoverable_mutation": [
                            "low-risk-cleanup",
                            "anomaly-repair",
                            "quarantine",
                            "restore",
                        ],
                        "maintenance_layers": [
                            "cleaner",
                            "shared",
                            "package",
                        ],
                        "forbidden": [
                            "outside-project",
                            "source-code-edit",
                            "git-tracked-file",
                            "force-unlock",
                            "process-termination",
                            "active-package-lock",
                            "current-dist",
                            "rollback-incomplete",
                            "dependency-directory",
                            "browser-profile",
                            "user-data",
                        ],
                    },
                    "message": "project cleaner status ready",
                }
