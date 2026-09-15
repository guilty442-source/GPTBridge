# Cleanup apply-pipeline mixin for the project-cleaner executor.
from __future__ import annotations

import os
import shutil
import stat as stat_module
from pathlib import Path
from typing import Any, Callable

from .cleanup_helpers import QUARANTINE_SCHEMA_VERSION


class CleanupGarbageApplyMixin:

        def _apply_cleanup_items(
            self,
            items: list[dict[str, Any]],
            document: dict[str, Any] | None,
            batch_dir: Path,
            quarantine: bool,
        ) -> dict[str, Any]:
            cleaned: dict[str, Any] = {
                "files": 0,
                "dirs": 0,
                "bytes": 0,
                "errors": [],
                "skips": [],
                "moved_entries": document["items"] if document is not None else [],
            }
            for index, item in enumerate(items):
                files, dirs, moved_bytes = self._apply_cleanup_item(
                    item, index, cleaned, document, batch_dir, quarantine, len(items)
                )
                cleaned["files"] += files
                cleaned["dirs"] += dirs
                cleaned["bytes"] += moved_bytes
            return cleaned

        def _apply_cleanup_item(
            self,
            item: dict[str, Any],
            index: int,
            cleaned: dict[str, Any],
            document: dict[str, Any] | None,
            batch_dir: Path,
            quarantine: bool,
            total: int,
        ) -> tuple[int, int, int]:
            rel_path = str(item.get("path") or "")
            item_type = str(item.get("type") or "")
            target = self.project_root / rel_path
            self._emit_progress(
                "apply",
                5 + int(index / max(1, total) * 90),
                "套用清理計畫",
                current_path=rel_path,
                completed=index,
                total=total,
            )
            if self._cleanup_item_precheck(item, target, rel_path, item_type, index, cleaned, document, batch_dir):
                return 0, 0, 0
            try:
                return self._execute_cleanup_move(
                    item, index, target, rel_path, item_type, cleaned, document, batch_dir, quarantine
                )
            except (OSError, ValueError) as exc:
                processes = self._locking_processes(target)
                cleaned["errors"].append(
                    {"path": rel_path, "type": item_type, "message": str(exc), "locked_by": processes}
                )
                if document is not None:
                    cleaned["moved_entries"][index]["status"] = "error"
                    cleaned["moved_entries"][index]["status_reason"] = str(exc)
                    document["errors"] = cleaned["errors"]
                    self._write_batch_document(batch_dir, document)
                return 0, 0, 0

        def _cleanup_item_precheck(
            self,
            item: dict[str, Any],
            target: Path,
            rel_path: str,
            item_type: str,
            index: int,
            cleaned: dict[str, Any],
            document: dict[str, Any] | None,
            batch_dir: Path,
        ) -> bool:
            safe, reason = self._safe_candidate(
                target, item_type, check_git=not bool(item.get("allow_tracked"))
            )
            if not safe:
                self._cleanup_item_skipped(item, index, cleaned, document, batch_dir, reason)
                return True
            try:
                current_snapshot = self._candidate_snapshot(target, item_type)
            except OSError as exc:
                cleaned["errors"].append({"path": rel_path, "type": item_type, "message": str(exc)})
                return True
            expected_fingerprint = item.get("fingerprint") if isinstance(item.get("fingerprint"), dict) else {}
            if current_snapshot.get("digest") != expected_fingerprint.get("digest"):
                self._cleanup_item_skipped(item, index, cleaned, document, batch_dir, "candidate changed after preview")
                return True
            locked_path = self._locked_descendant(target)
            if locked_path is not None:
                processes = self._locking_processes(locked_path)
                skip = {
                    "path": rel_path,
                    "type": item_type,
                    "reason": "file is in use; cleaner never forces unlock",
                    "locked_path": self._relative_path(locked_path),
                    "locked_by": processes,
                }
                cleaned["skips"].append(skip)
                if document is not None:
                    cleaned["moved_entries"][index]["status"] = "locked"
                    cleaned["moved_entries"][index]["locked_by"] = processes
                    document["skipped"] = cleaned["skips"]
                    self._write_batch_document(batch_dir, document)
                return True
            return False

        def _cleanup_item_skipped(
            self,
            item: dict[str, Any],
            index: int,
            cleaned: dict[str, Any],
            document: dict[str, Any] | None,
            batch_dir: Path,
            reason: str,
        ) -> None:
            cleaned["skips"].append(
                {"path": str(item.get("path") or ""), "type": str(item.get("type") or ""), "reason": reason}
            )
            if document is not None:
                cleaned["moved_entries"][index]["status"] = "skipped"
                cleaned["moved_entries"][index]["status_reason"] = reason
                document["skipped"] = cleaned["skips"]
                self._write_batch_document(batch_dir, document)

        def _execute_cleanup_move(
            self,
            item: dict[str, Any],
            index: int,
            target: Path,
            rel_path: str,
            item_type: str,
            cleaned: dict[str, Any],
            document: dict[str, Any] | None,
            batch_dir: Path,
            quarantine: bool,
        ) -> tuple[int, int, int]:
            size = int(item.get("size_bytes") or 0)
            contents_only = (
                item_type == "directory"
                and (
                    bool(self.rules.get("delete_files_only", True))
                    or item.get("contents_only") is True
                )
            )
            if quarantine and document is not None:
                content_file_count = self._quarantine_item_move(
                    item, index, target, item_type, contents_only, cleaned, batch_dir, document
                )
            else:
                content_file_count = self._direct_item_delete(
                    target, item_type, contents_only
                )
            if contents_only:
                return content_file_count, 0, size
            return int(item_type == "file"), int(item_type == "directory"), size

        def _quarantine_item_move(
            self,
            item: dict[str, Any],
            index: int,
            target: Path,
            item_type: str,
            contents_only: bool,
            cleaned: dict[str, Any],
            batch_dir: Path,
            document: dict[str, Any],
        ) -> int:
            quarantine_rel = str(cleaned["moved_entries"][index]["quarantine_path"])
            quarantine_target = (batch_dir / quarantine_rel).resolve()
            if not quarantine_target.is_relative_to(batch_dir.resolve()):
                raise ValueError(
                    f"quarantine target escaped batch directory: {quarantine_target}"
                )
            cleaned["moved_entries"][index]["status"] = "moving"
            self._write_batch_document(batch_dir, document)
            content_file_count = 0
            if contents_only:
                quarantine_target.mkdir(parents=True, exist_ok=False)
                content_files = self._regular_files_below(target)
                content_file_count = len(content_files)
                for source_file in content_files:
                    destination_file = (
                        quarantine_target / source_file.relative_to(target)
                    )
                    destination_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source_file), str(destination_file))
            else:
                quarantine_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(quarantine_target))
            cleaned["moved_entries"][index]["content_sha256"] = self._content_digest(quarantine_target, item_type)
            cleaned["moved_entries"][index]["status"] = "moved"
            self._write_batch_document(batch_dir, document)
            return content_file_count

        def _direct_item_delete(
            self,
            target: Path,
            item_type: str,
            contents_only: bool,
        ) -> int:
            content_file_count = 0
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
            return content_file_count

        def _finalize_cleanup_batch(
            self,
            document: dict[str, Any] | None,
            batch_dir: Path,
            batch_name: str,
            plan: dict[str, Any],
            apply_errors: list[dict[str, Any]],
        ) -> str:
            if document is None:
                return ""
            document["status"] = "completed_with_errors" if apply_errors else "completed"
            document["errors"] = apply_errors
            self._write_batch_document(batch_dir, document)
            manifest_path = str(batch_dir / "manifest.json")
            try:
                manifest_digest = self._file_sha256(Path(manifest_path))
            except OSError as exc:
                apply_errors.append(
                    {
                        "path": str(batch_dir),
                        "type": "recovery",
                        "message": f"tombstone publication failed: {exc}",
                    }
                )
                return manifest_path
            moved_entries = document["items"]
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
            return manifest_path

        def _cleanup_apply_result(
            self,
            plan: dict[str, Any],
            plan_id: str,
            quarantine: bool,
            direct_delete_requested: bool,
            batch_name: str,
            batch_dir: Path,
            manifest_path: str,
            items: list[dict[str, Any]],
            cleaned: dict[str, Any],
        ) -> dict[str, Any]:
            cleaned_files = cleaned["files"]
            cleaned_dirs = cleaned["dirs"]
            cleaned_bytes = cleaned["bytes"]
            apply_errors = cleaned["errors"]
            apply_skips = cleaned["skips"]
            action = "quarantine" if quarantine else "delete"
            result = self._cleanup_result_mapping(
                plan, plan_id, quarantine, direct_delete_requested,
                batch_dir, manifest_path, items, cleaned,
                self._summarize_items(items),
                self._cleanup_health(
                    str(plan.get("scope")), items, apply_skips, apply_errors
                ),
                action,
            )
            self._finish_cleanup_apply(
                result, action, plan, plan_id, quarantine,
                batch_name, cleaned_files, cleaned_dirs, cleaned_bytes,
                apply_skips, apply_errors,
            )
            return result

        def _finish_cleanup_apply(
            self,
            result: dict[str, Any],
            action: str,
            plan: dict[str, Any],
            plan_id: str,
            quarantine: bool,
            batch_name: str,
            cleaned_files: int,
            cleaned_dirs: int,
            cleaned_bytes: int,
            apply_skips: list[dict[str, Any]],
            apply_errors: list[dict[str, Any]],
        ) -> None:
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
            self._emit_progress(
                "apply", 100, "清理計畫已完成", cleaned_bytes=cleaned_bytes
            )

        @staticmethod
        def _cleanup_result_mapping(
            plan: dict[str, Any],
            plan_id: str,
            quarantine: bool,
            direct_delete_requested: bool,
            batch_dir: Path,
            manifest_path: str,
            items: list[dict[str, Any]],
            cleaned: dict[str, Any],
            summary: dict[str, Any],
            health: dict[str, Any],
            action: str,
        ) -> dict[str, Any]:
            cleaned_files = cleaned["files"]
            cleaned_dirs = cleaned["dirs"]
            cleaned_bytes = cleaned["bytes"]
            apply_errors = cleaned["errors"]
            apply_skips = cleaned["skips"]
            return {
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
    "summary": summary,
    "health": health,
    "skipped": apply_skips,
    "skipped_count": len(apply_skips),
    "errors": apply_errors,
    "error_count": len(apply_errors),
    "message": f"{plan.get('scope')} cleanup {action} completed (items={cleaned_files + cleaned_dirs})",
}
