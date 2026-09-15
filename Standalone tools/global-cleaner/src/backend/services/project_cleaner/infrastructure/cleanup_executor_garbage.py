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


class CleanupGarbageMixin:

        def cleanup_garbage(
            self,
            scope: str,
            dry_run: bool = False,
            *,
            quarantine: bool = False,
            quarantine_ttl_hours: int | None = None,
            plan_id: str = "",
            plan_token: str = "",
            selected_item_ids: list[str] | None = None,
            confirm_direct_delete: bool = False,
        ) -> dict[str, Any]:
            if dry_run:
                return self._cleanup_garbage_unlocked(
                    scope,
                    dry_run=True,
                    quarantine=quarantine,
                    quarantine_ttl_hours=quarantine_ttl_hours,
                    plan_id=plan_id,
                    plan_token=plan_token,
                    selected_item_ids=selected_item_ids,
                    confirm_direct_delete=confirm_direct_delete,
                )
            with self._mutation_guard("apply") as lock:
                if not lock.get("acquired"):
                    return {
                        "ok": False,
                        "busy": True,
                        "scope": str(scope or ""),
                        "dry_run": False,
                        "quarantine": quarantine,
                        "cleaned_files": 0,
                        "cleaned_dirs": 0,
                        "cleaned_bytes": 0,
                        "permanently_deleted": 0,
                        "message": str(lock.get("message") or "project cleaner is busy"),
                    }
                return self._cleanup_garbage_unlocked(
                    scope,
                    dry_run=False,
                    quarantine=quarantine,
                    quarantine_ttl_hours=quarantine_ttl_hours,
                    plan_id=plan_id,
                    plan_token=plan_token,
                    selected_item_ids=selected_item_ids,
                    confirm_direct_delete=confirm_direct_delete,
                )

        def _cleanup_garbage_unlocked(
            self,
            scope: str,
            dry_run: bool = False,
            *,
            quarantine: bool = False,
            quarantine_ttl_hours: int | None = None,
            plan_id: str = "",
            plan_token: str = "",
            selected_item_ids: list[str] | None = None,
            confirm_direct_delete: bool = False,
        ) -> dict[str, Any]:
            if dry_run:
                return self._cleanup_dry_run_result(scope, quarantine)
            direct_delete_requested = not quarantine
            plan, plan_error = self._load_plan(plan_id, plan_token)
            if plan is None:
                return {
                    "ok": False,
                    "scope": str(scope or ""),
                    "dry_run": False,
                    "quarantine": quarantine,
                    "cleaned_files": 0,
                    "cleaned_dirs": 0,
                    "cleaned_bytes": 0,
                    "message": plan_error,
                }
            denied, items = self._validate_cleanup_request(
                plan, scope, quarantine, confirm_direct_delete, selected_item_ids
            )
            if denied is not None:
                return denied
            ttl_hours = max(1, int(quarantine_ttl_hours or self._quarantine_ttl_hours()))
            created_at = datetime.now(timezone.utc)
            batch_name = f"{created_at.strftime('%Y%m%d_%H%M%S_%f')}-{uuid.uuid4().hex[:8]}"
            batch_dir = self.quarantine_root / batch_name
            document = None
            if quarantine:
                document = self._quarantine_batch_setup(
                    items, plan, plan_id, batch_name, batch_dir,
                    created_at, ttl_hours,
                )
            cleaned = self._apply_cleanup_items(items, document, batch_dir, quarantine)
            manifest_path = self._finalize_cleanup_batch(
                document, batch_dir, batch_name, plan, cleaned["errors"]
            )
            return self._cleanup_apply_result(
                plan, plan_id, quarantine, direct_delete_requested,
                batch_name, batch_dir, manifest_path, items, cleaned,
            )

        def _validate_cleanup_request(
            self,
            plan: dict[str, Any],
            scope: str,
            quarantine: bool,
            confirm_direct_delete: bool,
            selected_item_ids: list[str] | None,
        ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
            if not quarantine and not confirm_direct_delete:
                return {
                    "ok": False,
                    "scope": str(scope or ""),
                    "dry_run": False,
                    "quarantine": False,
                    "error_code": "DIRECT_DELETE_CONFIRMATION_REQUIRED",
                    "message": "direct deletion requires explicit confirmation",
                }, []
            if str(plan.get("scope")) != str(scope or "").strip().lower().replace("project", "global"):
                return {"ok": False, "message": "preview plan scope mismatch", "scope": scope}, []
            all_items = [item for item in plan.get("items", []) if isinstance(item, dict)]
            selected = {str(item) for item in (selected_item_ids or []) if str(item).strip()}
            items = [item for item in all_items if not selected or str(item.get("item_id")) in selected]
            if selected and len(items) != len(selected):
                return {"ok": False, "message": "selected cleanup items do not match preview plan", "scope": scope}, []
            if not quarantine and any(
                str(item.get("risk") or "") != "low"
                and item.get("allow_direct_delete") is not True
                for item in items
            ):
                return {
                    "ok": False,
                    "scope": str(scope or ""),
                    "dry_run": False,
                    "quarantine": False,
                    "error_code": "DIRECT_DELETE_RISK_DENIED",
                    "message": "permanent deletion accepts selected low-risk items only",
                }, []
            return None, items

        def _quarantine_batch_setup(
            self,
            items: list[dict[str, Any]],
            plan: dict[str, Any],
            plan_id: str,
            batch_name: str,
            batch_dir: Path,
            created_at: datetime,
            ttl_hours: int,
        ) -> dict[str, Any]:
            batch_dir.mkdir(parents=True, exist_ok=False)
            self._harden_private_path(batch_dir)
            journal_items: list[dict[str, Any]] = []
            reserved_paths: set[str] = set()
            for item in items:
                rel_path = str(item.get("path") or "")
                candidate = (Path("items") / rel_path).as_posix()
                if (
                    not rel_path
                    or Path(rel_path).is_absolute()
                    or ".." in Path(rel_path).parts
                    or candidate.casefold() in reserved_paths
                ):
                    item_id = str(item.get("item_id") or uuid.uuid4().hex)
                    candidate = (Path("items") / item_id / Path(rel_path).name).as_posix()
                reserved_paths.add(candidate.casefold())
                journal_items.append(
                    {
                        **item,
                        "status": "pending",
                        "quarantine_path": candidate,
                        "content_sha256": "",
                    }
                )
            document = {
                "schema_version": QUARANTINE_SCHEMA_VERSION,
                "batch_id": batch_name,
                "status": "applying",
                "created_at": created_at.isoformat(),
                "expires_at": (created_at + timedelta(hours=ttl_hours)).isoformat(),
                "pinned": False,
                "project_root": str(self.project_root),
                "scope": plan.get("scope"),
                "plan_id": plan_id,
                "items": journal_items,
                "errors": [],
                "skipped": [],
            }
            self._write_batch_document(batch_dir, document)
            return document

        def _cleanup_dry_run_result(
            self, scope: str, quarantine: bool
        ) -> dict[str, Any]:
            plan = self.plan_cleanup(scope)
            return {
                **plan,
                "dry_run": True,
                "quarantine": quarantine,
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "planned_files": int(plan.get("file_count") or 0),
                "planned_dirs": int(plan.get("dir_count") or 0),
                "planned_bytes": int(plan.get("total_bytes") or 0),
            }
