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


class CleanupPlannerMixin:
    def _load_rules(self) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        bundled_path = Path(__file__).resolve().parent.parent / "domain" / "cleanup_rules.json"
        rules = json.loads(json.dumps(FALLBACK_RULES))
        if bundled_path.exists():
            try:
                payload = json.loads(bundled_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    rules = _deep_merge(rules, payload)
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"bundled rule file invalid: {exc}")
        if self.rules_override_path.exists():
            try:
                payload = json.loads(self.rules_override_path.read_text(encoding="utf-8"))
                list_fields = (
                    "excluded_directory_names",
                    "protected_relative_paths",
                    "directory_rules",
                    "file_rules",
                )
                valid_override = (
                    isinstance(payload, dict)
                    and int(payload.get("schema_version") or 0) == PLAN_SCHEMA_VERSION
                    and all(
                        field not in payload or isinstance(payload.get(field), list)
                        for field in list_fields
                    )
                    and all(
                        field not in payload or isinstance(payload.get(field), dict)
                        for field in ("analysis",)
                    )
                )
                if valid_override:
                    rules = _deep_merge(rules, payload)
                else:
                    warnings.append(
                        "rule override ignored: schema or field types are invalid"
                    )
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"rule override invalid: {exc}")
        excluded = {
            str(item)
            for item in rules.get("excluded_directory_names", [])
            if str(item).strip()
        }
        protected = {
            str(item).replace("\\", "/").strip("/")
            for item in rules.get("protected_relative_paths", [])
            if str(item).strip()
        }
        rules["excluded_directory_names"] = sorted(
            excluded | CORE_EXCLUDED_DIRECTORY_NAMES,
            key=str.casefold,
        )
        rules["protected_relative_paths"] = sorted(
            protected | CORE_PROTECTED_RELATIVE_PATHS,
            key=str.casefold,
        )
        if int(rules.get("schema_version") or 0) != PLAN_SCHEMA_VERSION:
            warnings.append("rule schema version does not match cleaner schema")
        return rules, warnings


    @staticmethod
    def _fsync_parent_directory(directory: Path) -> None:
        """Best-effort persistence barrier for a completed atomic replace."""

        if os.name == "nt":
            try:
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                create_file = kernel32.CreateFileW
                create_file.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ]
                create_file.restype = wintypes.HANDLE
                flush_file_buffers = kernel32.FlushFileBuffers
                flush_file_buffers.argtypes = [wintypes.HANDLE]
                flush_file_buffers.restype = wintypes.BOOL
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = [wintypes.HANDLE]
                close_handle.restype = wintypes.BOOL

                file_share_all = 0x00000001 | 0x00000002 | 0x00000004
                open_existing = 3
                file_flag_backup_semantics = 0x02000000
                handle = create_file(
                    str(directory),
                    0,
                    file_share_all,
                    None,
                    open_existing,
                    file_flag_backup_semantics,
                    None,
                )
                invalid_handle = ctypes.c_void_p(-1).value
                if handle not in (None, 0, invalid_handle):
                    try:
                        flush_file_buffers(handle)
                    finally:
                        close_handle(handle)
                return
            except (OSError, AttributeError, ValueError):
                return

        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(str(directory), flags)
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)


    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            with temp_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as target:
                target.write(serialized)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, path)
            self._fsync_parent_directory(path.parent)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


    def _quarantine_ttl_hours(self) -> int:
        return max(1, int(self.rules.get("quarantine_ttl_hours") or DEFAULT_QUARANTINE_TTL_HOURS))


    def _plan_ttl_minutes(self) -> int:
        return max(1, int(self.rules.get("plan_ttl_minutes") or DEFAULT_PLAN_TTL_MINUTES))


    def update_preferences(
        self,
        *,
        quarantine_ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        with self._mutation_guard("preferences") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._update_preferences_unlocked(
                quarantine_ttl_hours=quarantine_ttl_hours,
            )


    def _update_preferences_unlocked(
        self,
        *,
        quarantine_ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        override: dict[str, Any] = {}
        if self.rules_override_path.exists():
            try:
                loaded = json.loads(self.rules_override_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    override = loaded
            except (OSError, json.JSONDecodeError) as exc:
                return {"ok": False, "message": f"rule override is invalid: {exc}"}
        override.pop("automation", None)
        if quarantine_ttl_hours is not None:
            override["quarantine_ttl_hours"] = max(1, min(24 * 365, int(quarantine_ttl_hours)))
        override.setdefault("schema_version", PLAN_SCHEMA_VERSION)
        self._atomic_write_json(self.rules_override_path, override)
        self.rules, self.rule_warnings = self._load_rules()
        self._append_history(
            "preferences",
            ok=True,
            quarantine_ttl_hours=self._quarantine_ttl_hours(),
        )
        return {
            "ok": True,
            "override_path": str(self.rules_override_path),
            "quarantine_ttl_hours": self._quarantine_ttl_hours(),
            "message": "project cleaner preferences updated",
        }


    def _plan_key(self) -> bytes:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._harden_private_path(self.runtime_root)
        if self.key_path.exists():
            return self.key_path.read_bytes()
        key = secrets.token_bytes(32)
        try:
            with self.key_path.open("xb") as target:
                target.write(key)
            try:
                self.key_path.chmod(0o600)
            except OSError:
                pass
            return key
        except FileExistsError:
            return self.key_path.read_bytes()


    def _plan_signature(self, payload: dict[str, Any]) -> str:
        signable = {key: value for key, value in payload.items() if key != "plan_token"}
        encoded = json.dumps(
            signable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._plan_key(), encoded, hashlib.sha256).hexdigest()


    def _persist_plan(self, plan: dict[str, Any]) -> tuple[str, str]:
        plan_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        document = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_id": plan_id,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(minutes=self._plan_ttl_minutes())).isoformat(),
            **plan,
        }
        token = self._plan_signature(document)
        document["plan_token"] = token
        self._atomic_write_json(self.plan_root / f"{plan_id}.json", document)
        return plan_id, token


    def _load_plan(self, plan_id: str, plan_token: str) -> tuple[dict[str, Any] | None, str]:
        clean_id = str(plan_id or "").strip().lower()
        if len(clean_id) != 32 or any(char not in "0123456789abcdef" for char in clean_id):
            return None, "valid preview plan is required"
        path = self.plan_root / f"{clean_id}.json"
        if not path.exists():
            return None, "preview plan not found"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"preview plan is invalid: {exc}"
        if not isinstance(document, dict):
            return None, "preview plan is invalid"
        expected = self._plan_signature(document)
        stored = str(document.get("plan_token") or "")
        supplied = str(plan_token or "")
        if not stored or not supplied or not hmac.compare_digest(expected, stored) or not hmac.compare_digest(stored, supplied):
            return None, "preview plan signature mismatch"
        expires_at = _parse_iso(str(document.get("expires_at") or ""))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            return None, "preview plan expired; run preview again"
        return document, ""


    def plan_cleanup(self, scope: str, *, now: float | None = None) -> dict[str, Any]:
        requested_scope = str(scope or "global").strip().lower()
        normalized_scope, candidate_roots = self._candidate_roots(requested_scope)
        if candidate_roots is None:
            return {
                "ok": False,
                "scope": requested_scope,
                "requested_scope": requested_scope,
                "items": [],
                "skipped": [],
                "errors": [],
                "message": f"unsupported cleanup scope: {requested_scope}",
            }
        self._emit_progress("scan", 2, "建立清理計畫", scope=normalized_scope)
        now = time.time() if now is None else now
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        excluded_names = {
            str(item).casefold() for item in self.rules.get("excluded_directory_names", [])
        }
        _tracked, git_status = self._git_snapshot()
        if git_status.get("repository") and not git_status.get("available"):
            errors.append({"path": ".git", "type": "git", "message": git_status.get("error") or "git unavailable"})

        for root_index, raw_root in enumerate(candidate_roots):
            root = self._safe_resolve(raw_root)
            if not root.exists():
                continue
            if not self._inside_project(root) or self._is_protected(root):
                self._append_skip(skipped, {"path": str(raw_root), "reason": "protected or outside project"})
                continue
            for current_raw, dirnames, filenames in os.walk(root, followlinks=False):
                current = Path(current_raw)
                if self._is_protected(current):
                    dirnames[:] = []
                    continue
                kept: list[str] = []
                for dirname in sorted(dirnames):
                    child = current / dirname
                    rule = self._directory_rule(
                        dirname,
                        path=child,
                        scope=normalized_scope,
                    )
                    if (
                        rule is None
                        and dirname.casefold() in excluded_names
                    ) or self._is_protected(child):
                        continue
                    if self._is_link_or_reparse_point(child):
                        self._append_skip(skipped, {"path": self._relative_path(child), "type": "directory", "reason": "link or reparse point"})
                        continue
                    if rule is None:
                        kept.append(dirname)
                        continue
                    min_age_days = max(0.0, float(rule.get("min_age_days") or 0))
                    if self._age_days(child, now) < min_age_days:
                        continue
                    safe, reason = self._safe_candidate(child, "directory")
                    if safe and bool(rule.get("protect_user_content")) and self._directory_contains_user_content(child):
                        safe, reason = False, "directory contains source-like user content"
                    if not safe:
                        self._append_skip(skipped, {"path": self._relative_path(child), "type": "directory", "reason": reason})
                        continue
                    try:
                        snapshot = self._candidate_snapshot(child, "directory")
                        rel_path = self._relative_path(child)
                        item_id = hashlib.sha256(f"directory:{rel_path}:{snapshot['digest']}".encode("utf-8")).hexdigest()[:24]
                        contents_only = bool(
                            self.rules.get("delete_files_only", True)
                        ) or bool(rule.get("contents_only"))
                        if contents_only and int(
                            snapshot.get("file_count") or 0
                        ) == 0:
                            continue
                        items.append(
                            {
                                "item_id": item_id,
                                "path": rel_path,
                                "type": "directory",
                                "size_bytes": snapshot["size_bytes"],
                                "entry_count": snapshot["entry_count"],
                                "reason": str(rule.get("reason") or "generated directory"),
                                "rule_id": str(rule.get("id") or "directory-rule"),
                                "risk": str(rule.get("risk") or "low"),
                                "age_days": round(self._age_days(child, now), 2),
                                "min_age_days": min_age_days,
                                "contents_only": contents_only,
                                "allow_direct_delete": bool(
                                    rule.get("allow_direct_delete")
                                ),
                                "fingerprint": snapshot,
                            }
                        )
                    except OSError as exc:
                        errors.append({"path": self._relative_path(child), "type": "directory", "message": str(exc)})
                    continue
                dirnames[:] = kept

                for filename in sorted(filenames):
                    path = current / filename
                    if self._is_protected(path) or self._is_link_or_reparse_point(path):
                        continue
                    rule = self._file_rule(path, now)
                    if rule is None:
                        continue
                    safe, reason = self._safe_candidate(
                        path,
                        "file",
                        check_git=not bool(rule.get("allow_tracked")),
                    )
                    if not safe:
                        self._append_skip(skipped, {"path": self._relative_path(path), "type": "file", "reason": reason})
                        continue
                    try:
                        snapshot = self._candidate_snapshot(path, "file")
                        rel_path = self._relative_path(path)
                        item_id = hashlib.sha256(f"file:{rel_path}:{snapshot['digest']}".encode("utf-8")).hexdigest()[:24]
                        items.append(
                            {
                                "item_id": item_id,
                                "path": rel_path,
                                "type": "file",
                                "size_bytes": snapshot["size_bytes"],
                                "entry_count": 1,
                                "reason": str(rule.get("reason") or "generated file"),
                                "rule_id": str(rule.get("id") or "file-rule"),
                                "risk": str(rule.get("risk") or "low"),
                                "allow_tracked": bool(rule.get("allow_tracked")),
                                "allow_direct_delete": bool(
                                    rule.get("allow_direct_delete")
                                ),
                                "age_days": round(self._age_days(path, now), 2),
                                "min_age_days": max(0.0, float(rule.get("min_age_days") or 0)),
                                "fingerprint": snapshot,
                            }
                        )
                    except OSError as exc:
                        errors.append({"path": self._relative_path(path), "type": "file", "message": str(exc)})
            self._emit_progress(
                "scan",
                15 + int((root_index + 1) / max(1, len(candidate_roots)) * 65),
                "掃描清理候選",
                item_count=len(items),
            )

        total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
        file_count = sum(1 for item in items if item.get("type") == "file")
        dir_count = sum(1 for item in items if item.get("type") == "directory")
        health = self._cleanup_health(normalized_scope, items, skipped, errors)
        plan_payload = {
            "scope": normalized_scope,
            "requested_scope": requested_scope,
            "items": items,
            "item_count": len(items),
            "file_count": file_count,
            "dir_count": dir_count,
            "total_bytes": total_bytes,
            "summary": self._summarize_items(items),
            "health": health,
            "skipped": skipped,
            "skipped_count": len(skipped),
            "errors": errors,
            "error_count": len(errors),
            "git": git_status,
            "rule_schema_version": self.rules.get("schema_version"),
        }
        plan_id, plan_token = self._persist_plan(plan_payload)
        self._append_history(
            "preview",
            ok=True,
            scope=normalized_scope,
            plan_id=plan_id,
            item_count=len(items),
            bytes=total_bytes,
        )
        self._emit_progress("scan", 100, "清理計畫完成", plan_id=plan_id, item_count=len(items))
        return {
            "ok": True,
            **plan_payload,
            "plan_id": plan_id,
            "plan_token": plan_token,
            "plan_expires_in_minutes": self._plan_ttl_minutes(),
            "message": f"{normalized_scope} cleanup plan completed (items={len(items)})",
        }
