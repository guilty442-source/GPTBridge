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


class CleanupQuarantineMixin:

        def list_quarantine_batches(self) -> dict[str, Any]:
            batches: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            batch_roots = [
                (self.quarantine_root, False),
                (self.recovery_root / "purged", True),
            ]
            if not any(root.exists() for root, _archived in batch_roots):
                return {
                    "ok": True,
                    "batches": [],
                    "batch_count": 0,
                    "total_bytes": 0,
                    "health": self._quarantine_health([]),
                    "errors": [],
                    "message": "quarantine is empty",
                }
            now = datetime.now(timezone.utc)
            for batch_root, archived in batch_roots:
                self._collect_batch_summaries(
                    batch_root, archived, now, batches, errors
                )
            return {
                "ok": not errors,
                "batches": batches,
                "batch_count": len(batches),
                "total_bytes": sum(int(item.get("size_bytes") or 0) for item in batches),
                "health": self._quarantine_health(batches),
                "errors": errors,
                "message": f"quarantine batches listed (items={len(batches)})",
            }

        def _quarantine_health(self, batches: list[dict[str, Any]]) -> dict[str, Any]:
            total_bytes = sum(int(batch.get("size_bytes") or 0) for batch in batches)
            expired_count = sum(1 for batch in batches if batch.get("expired") and not batch.get("pinned"))
            incomplete_count = sum(1 for batch in batches if batch.get("recoverable"))
            score = max(0, 100 - expired_count * 10 - incomplete_count * 25)
            if not batches:
                state, recommendation = "empty", "隔離區目前沒有批次。"
            elif incomplete_count:
                state, recommendation = "attention", "有中斷交易，建議先還原或完成處理。"
            elif expired_count:
                state, recommendation = "attention", "有過期批次可清理；釘選批次不會自動移除。"
            else:
                state, recommendation = "healthy", "隔離交易完整且可還原。"
            return {
                "state": state,
                "score": score,
                "batch_count": len(batches),
                "expired_count": expired_count,
                "incomplete_count": incomplete_count,
                "pinned_count": sum(1 for batch in batches if batch.get("pinned")),
                "total_bytes": total_bytes,
                "ttl_hours": self._quarantine_ttl_hours(),
                "recommendation": recommendation,
            }

        def set_quarantine_pinned(self, batch_name: str, pinned: bool) -> dict[str, Any]:
            clean_name = Path(str(batch_name or "").strip()).name
            if not clean_name:
                return {"ok": False, "message": "quarantine batch is required"}
            with self._mutation_guard("pin" if pinned else "unpin", batch_name=clean_name) as lock:
                if not lock.get("acquired"):
                    return {
                        "ok": False,
                        "busy": True,
                        "message": str(lock.get("message") or "project cleaner is busy"),
                    }
                return self._set_quarantine_pinned_unlocked(clean_name, pinned)

        def _set_quarantine_pinned_unlocked(
            self, clean_name: str, pinned: bool
        ) -> dict[str, Any]:
            batch_dir = self.quarantine_root / clean_name
            if not batch_dir.exists():
                archived = self.recovery_root / "purged" / clean_name
                if archived.exists():
                    batch_dir = archived
            document, error = self._read_batch_document(batch_dir)
            if document is None:
                return {"ok": False, "message": error}
            document["pinned"] = bool(pinned)
            self._write_batch_document(batch_dir, document)
            self._append_history("pin" if pinned else "unpin", ok=True, batch_id=clean_name)
            return {"ok": True, "batch": clean_name, "pinned": bool(pinned), "message": "quarantine pin updated"}
















        def _collect_batch_summaries(
            self,
            batch_root: Path,
            archived: bool,
            now: datetime,
            batches: list[dict[str, Any]],
            errors: list[dict[str, str]],
        ) -> None:
            if not batch_root.exists():
                return
            for child in sorted(batch_root.iterdir(), key=lambda item: item.name, reverse=True):
                if self._is_link_or_reparse_point(child) or not child.is_dir():
                    continue
                document, error = self._read_batch_document(child)
                if document is None:
                    errors.append({"path": child.name, "message": error})
                    continue
                items = [item for item in document.get("items", []) if isinstance(item, dict)]
                size_bytes = sum(int(item.get("size_bytes") or 0) for item in items if self._is_restorable_item(item))
                expiration = self._batch_expiration(document, child)
                created = _parse_iso(str(document.get("created_at") or "")) or now
                recoverable_items = [item for item in items if self._is_restorable_item(item)]
                batches.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "archived": archived,
                        "created_at": created.isoformat(),
                        "scope": str(document.get("scope") or ""),
                        "status": str(document.get("status") or "legacy"),
                        "item_count": len(items),
                        "restorable_count": len(recoverable_items),
                        "size_bytes": size_bytes,
                        "age_hours": round(max(0.0, (now - created).total_seconds() / 3600), 2),
                        "expires_at": expiration.isoformat(),
                        "expired": False if archived else now >= expiration,
                        "pinned": bool(document.get("pinned")),
                        "recoverable": str(document.get("status")) == "applying"
                        or any(str(item.get("status") or "") in {"pending", "error"} for item in recoverable_items),
                        "manifest_path": str(self._batch_document_path(child) or ""),
                    }
                )
