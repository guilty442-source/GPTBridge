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


class CleanupRecordsMixin:

        @staticmethod
        def _iso_now() -> str:
            return datetime.now(timezone.utc).isoformat()

        @staticmethod
        def _age_days(path: Path, now: float) -> float:
            try:
                return max(0.0, (now - path.stat(follow_symlinks=False).st_mtime) / SECONDS_PER_DAY)
            except OSError:
                return 0.0

        def _emit_progress(self, phase: str, percent: int, message: str, **payload: Any) -> None:
            if self.progress_callback is None:
                return
            event = {
                "phase": phase,
                "percent": max(0, min(100, int(percent))),
                "message": message,
                "timestamp": self._iso_now(),
                **payload,
            }
            try:
                self.progress_callback(event)
            except Exception:
                pass

        def _append_history(self, action: str, **payload: Any) -> None:
            self.business_history.append(action, **payload)

        def _history_records(self, *, limit: int = 50) -> list[dict[str, Any]]:
            return self.business_history.read(limit=limit)

        def _unique_quarantine_target(self, batch_dir: Path, rel_path: str) -> Path:
            target = batch_dir / "items" / rel_path
            if not target.exists():
                return target
            for index in range(1, 1000):
                candidate = target.with_name(f"{target.stem}.{index}{target.suffix}")
                if not candidate.exists():
                    return candidate
            return target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}{target.suffix}")

        @staticmethod
        def _batch_document_sort_key(
            path: Path,
            payload: dict[str, Any],
        ) -> tuple[int, float, int]:
            try:
                revision = max(0, int(payload.get("revision") or 0))
            except (TypeError, ValueError):
                revision = 0
            updated_at = _parse_iso(str(payload.get("updated_at") or ""))
            updated_timestamp = updated_at.timestamp() if updated_at is not None else 0.0
            # The journal is the write-ahead record, so prefer it only when both
            # monotonic revision and timestamp are otherwise identical.
            journal_tiebreaker = int(path.name == "journal.json")
            return revision, updated_timestamp, journal_tiebreaker

        def _valid_batch_documents(
            self,
            batch_dir: Path,
        ) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
            valid: list[tuple[Path, dict[str, Any]]] = []
            errors: list[str] = []
            found = False
            for path in (batch_dir / "manifest.json", batch_dir / "journal.json"):
                if not path.exists():
                    continue
                found = True
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"{path.name}: {exc}")
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    errors.append(f"{path.name}: invalid items")
                    continue
                valid.append((path, payload))
            if not found:
                errors.append("quarantine manifest not found")
            return valid, errors

        def _batch_document_path(self, batch_dir: Path) -> Path | None:
            valid, _errors = self._valid_batch_documents(batch_dir)
            if not valid:
                return None
            return max(
                valid,
                key=lambda item: self._batch_document_sort_key(item[0], item[1]),
            )[0]

        def _read_batch_document(self, batch_dir: Path) -> tuple[dict[str, Any] | None, str]:
            valid, errors = self._valid_batch_documents(batch_dir)
            if not valid:
                if errors == ["quarantine manifest not found"]:
                    return None, errors[0]
                return None, f"invalid quarantine documents: {'; '.join(errors)}"
            _path, payload = max(
                valid,
                key=lambda item: self._batch_document_sort_key(item[0], item[1]),
            )
            return payload, ""

        def _write_batch_document(self, batch_dir: Path, document: dict[str, Any]) -> None:
            persisted, _error = self._read_batch_document(batch_dir)
            persisted_revision = 0
            if persisted is not None:
                try:
                    persisted_revision = max(0, int(persisted.get("revision") or 0))
                except (TypeError, ValueError):
                    persisted_revision = 0
            try:
                document_revision = max(0, int(document.get("revision") or 0))
            except (TypeError, ValueError):
                document_revision = 0
            document["revision"] = max(persisted_revision, document_revision) + 1
            document["updated_at"] = self._iso_now()
            self._atomic_write_json(batch_dir / "journal.json", document)
            if str(document.get("status") or "legacy") != "applying":
                self._atomic_write_json(batch_dir / "manifest.json", document)

        @staticmethod
        def _is_restorable_item(item: dict[str, Any]) -> bool:
            status = str(item.get("status") or "")
            return bool(item.get("quarantine_path")) and (
                status in {"moved", "moving", "pending", "error"}
                or (not status and bool(item.get("original_path")))
            )

        def _batch_expiration(self, batch: dict[str, Any], batch_dir: Path) -> datetime:
            expires = _parse_iso(str(batch.get("expires_at") or ""))
            if expires is not None:
                return expires
            try:
                modified = datetime.fromtimestamp(batch_dir.stat(follow_symlinks=False).st_mtime, timezone.utc)
            except OSError:
                modified = datetime.now(timezone.utc)
            return modified + timedelta(hours=self._quarantine_ttl_hours())

        @staticmethod
        def _unique_restore_destination(destination: Path) -> Path:
            for index in range(1, 1000):
                candidate = destination.with_name(f"{destination.stem}.restored-{index}{destination.suffix}")
                if not candidate.exists():
                    return candidate
            return destination.with_name(f"{destination.stem}.restored-{uuid.uuid4().hex[:8]}{destination.suffix}")
