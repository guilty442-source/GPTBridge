from __future__ import annotations

import os
import shutil
import stat as stat_module
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .cleanup_constants import QUARANTINE_SCHEMA_VERSION


class GarbageMixin:
    """Garbage cleanup: quarantine or direct-delete based on a signed preview plan."""

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
        if direct_delete_requested and not confirm_direct_delete:
            return {
                "ok": False,
                "scope": str(scope or ""),
                "dry_run": False,
                "quarantine": False,
                "error_code": "DIRECT_DELETE_CONFIRMATION_REQUIRED",
                "message": "direct deletion requires explicit confirmation",
            }
        if str(plan.get("scope")) != str(scope or "").strip().lower().replace("project", "global"):
            return {"ok": False, "message": "preview plan scope mismatch", "scope": scope}

        all_items = [item for item in plan.get("items", []) if isinstance(item, dict)]
        selected = {str(item) for item in (selected_item_ids or []) if str(item).strip()}
        items = [item for item in all_items if not selected or str(item.get("item_id")) in selected]
        if selected and len(items) != len(selected):
            return {"ok": False, "message": "selected cleanup items do not match preview plan", "scope": scope}
        if direct_delete_requested and any(
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
            }
        ttl_hours = max(1, int(quarantine_ttl_hours or self._quarantine_ttl_hours()))
        created_at = datetime.now(timezone.utc)
        batch_name = f"{created_at.strftime('%Y%m%d_%H%M%S_%f')}-{uuid.uuid4().hex[:8]}"
        batch_dir = self.quarantine_root / batch_name
        document: dict[str, Any] | None = None
        if quarantine:
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

        cleaned_files = 0
        cleaned_dirs = 0
        cleaned_bytes = 0
        apply_errors: list[dict[str, Any]] = []
        apply_skips: list[dict[str, Any]] = []
        moved_entries = document["items"] if document is not None else []
        for index, item in enumerate(items):
            rel_path = str(item.get("path") or "")
            item_type = str(item.get("type") or "")
            target = self.project_root / rel_path
            self._emit_progress(
                "apply",
                5 + int(index / max(1, len(items)) * 90),
                "套用清理計畫",
                current_path=rel_path,
                completed=index,
                total=len(items),
            )
            safe, reason = self._safe_candidate(
                target,
                item_type,
                check_git=not bool(item.get("allow_tracked")),
            )
            if not safe:
                skip = {"path": rel_path, "type": item_type, "reason": reason}
                apply_skips.append(skip)
                if document is not None:
                    moved_entries[index]["status"] = "skipped"
                    moved_entries[index]["status_reason"] = reason
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            try:
                current_snapshot = self._candidate_snapshot(target, item_type)
            except OSError as exc:
                apply_errors.append({"path": rel_path, "type": item_type, "message": str(exc)})
                continue
            expected_fingerprint = item.get("fingerprint") if isinstance(item.get("fingerprint"), dict) else {}
            if current_snapshot.get("digest") != expected_fingerprint.get("digest"):
                reason = "candidate changed after preview"
                apply_skips.append({"path": rel_path, "type": item_type, "reason": reason})
                if document is not None:
                    moved_entries[index]["status"] = "skipped"
                    moved_entries[index]["status_reason"] = reason
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            locked_path = self._locked_descendant(target)
            if locked_path is not None:
                processes = self._locking_processes(locked_path)
                reason = "file is in use; cleaner never forces unlock"
                apply_skips.append(
                    {
                        "path": rel_path,
                        "type": item_type,
                        "reason": reason,
                        "locked_path": self._relative_path(locked_path),
                        "locked_by": processes,
                    }
                )
                if document is not None:
                    moved_entries[index]["status"] = "locked"
                    moved_entries[index]["locked_by"] = processes
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            try:
                size = int(item.get("size_bytes") or 0)
                contents_only = (
                    item_type == "directory"
                    and (
                        bool(self.rules.get("delete_files_only", True))
                        or item.get("contents_only") is True
                    )
                )
                content_file_count = 0
                if quarantine:
                    quarantine_rel = str(moved_entries[index]["quarantine_path"])
                    quarantine_target = (batch_dir / quarantine_rel).resolve()
                    quarantine_target.relative_to(batch_dir.resolve())
                    moved_entries[index]["status"] = "moving"
                    self._write_batch_document(batch_dir, document)
                    if contents_only:
                        quarantine_target.mkdir(parents=True, exist_ok=False)
                        content_files = self._regular_files_below(target)
                        content_file_count = len(content_files)
                        for source_file in content_files:
                            destination_file = (
                                quarantine_target
                                / source_file.relative_to(target)
                            )
                            destination_file.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(source_file), str(destination_file))
                    else:
                        quarantine_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(quarantine_target))
                    moved_entries[index]["content_sha256"] = self._content_digest(quarantine_target, item_type)
                    moved_entries[index]["status"] = "moved"
                    self._write_batch_document(batch_dir, document)
                else:
                    if item_type == "directory":
                        def clear_readonly_and_retry(
                            operation: Callable[..., Any],
                            path: str,
                            _error: tuple[type[BaseException], BaseException, Any],
                        ) -> None:
                            os.chmod(path, stat_module.S_IWRITE)
                            operation(path)

                        if contents_only:
                            content_files = self._regular_files_below(target)
                            content_file_count = len(content_files)
                            for content_file in content_files:
                                try:
                                    content_file.unlink()
                                except PermissionError:
                                    os.chmod(content_file, stat_module.S_IWRITE)
                                    content_file.unlink()
                        else:
                            shutil.rmtree(target, onerror=clear_readonly_and_retry)
                    else:
                        target.unlink()
                if contents_only:
                    cleaned_files += content_file_count
                else:
                    cleaned_dirs += int(item_type == "directory")
                    cleaned_files += int(item_type == "file")
                cleaned_bytes += size
            except OSError as exc:
                processes = self._locking_processes(target)
                error = {"path": rel_path, "type": item_type, "message": str(exc), "locked_by": processes}
                apply_errors.append(error)
                if document is not None:
                    moved_entries[index]["status"] = "error"
                    moved_entries[index]["status_reason"] = str(exc)
                    document["errors"] = apply_errors
                    self._write_batch_document(batch_dir, document)

        manifest_path = ""
        if document is not None:
            document["status"] = "completed_with_errors" if apply_errors else "completed"
            document["errors"] = apply_errors
            document["skipped"] = apply_skips
            self._write_batch_document(batch_dir, document)
            manifest_path = str(batch_dir / "manifest.json")
            try:
                manifest_digest = self._file_sha256(Path(manifest_path))
                tombstone = {
                    "schema_version": QUARANTINE_SCHEMA_VERSION,
                    "kind": "cleanup-tombstone",
                    "status": "recoverable",
                    "created_at": self._iso_now(),
                    "batch_id": batch_name,
                    "scope": plan.get("scope"),
                    "manifest_path": manifest_path,
                    "manifest_sha256": manifest_digest,
                    "items": [
                        {
                            "original_path": str(item.get("path") or ""),
                            "recovery_path": str(item.get("quarantine_path") or ""),
                            "content_sha256": str(item.get("content_sha256") or ""),
                            "type": str(item.get("type") or ""),
                        }
                        for item in moved_entries
                        if str(item.get("status") or "") == "moved"
                    ],
                }
                self._atomic_write_json(batch_dir / "tombstone.json", tombstone)
            except OSError as exc:
                apply_errors.append(
                    {
                        "path": str(batch_dir),
                        "type": "recovery",
                        "message": f"tombstone publication failed: {exc}",
                    }
                )
        action = "quarantine" if quarantine else "delete"
        result = {
            "ok": not apply_errors,
            "scope": plan.get("scope"),
            "requested_scope": plan.get("requested_scope"),
            "plan_id": plan_id,
            "dry_run": False,
            "quarantine": quarantine,
            "direct_delete_requested": direct_delete_requested,
            "direct_delete_prevented": False,
            "permanently_deleted": (
                cleaned_files + cleaned_dirs if not quarantine else 0
            ),
            "quarantine_path": str(batch_dir) if quarantine else "",
            "quarantine_manifest": manifest_path,
            "cleaned_files": cleaned_files,
            "cleaned_dirs": cleaned_dirs,
            "cleaned_bytes": cleaned_bytes,
            "retained_bytes": cleaned_bytes if quarantine else 0,
            "disk_space_reclaimed_bytes": cleaned_bytes if not quarantine else 0,
            "planned_files": sum(1 for item in items if item.get("type") == "file"),
            "planned_dirs": sum(1 for item in items if item.get("type") == "directory"),
            "planned_bytes": sum(int(item.get("size_bytes") or 0) for item in items),
            "items": items,
            "summary": self._summarize_items(items),
            "health": self._cleanup_health(str(plan.get("scope")), items, apply_skips, apply_errors),
            "skipped": apply_skips,
            "skipped_count": len(apply_skips),
            "errors": apply_errors,
            "error_count": len(apply_errors),
            "message": f"{plan.get('scope')} cleanup {action} completed (items={cleaned_files + cleaned_dirs})",
        }
        self._append_history(
            action,
            ok=result["ok"],
            scope=plan.get("scope"),
            plan_id=plan_id,
            item_count=cleaned_files + cleaned_dirs,
            bytes=cleaned_bytes,
            skipped=len(apply_skips),
            errors=len(apply_errors),
            batch_id=batch_name if quarantine else "",
        )
        try:
            (self.plan_root / f"{plan_id}.json").unlink(missing_ok=True)
        except OSError:
            pass
        self._emit_progress("apply", 100, "清理計畫已完成", cleaned_bytes=cleaned_bytes)
        return result
