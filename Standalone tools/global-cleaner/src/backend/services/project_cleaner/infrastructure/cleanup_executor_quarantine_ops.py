# Quarantine purge/restore mixin for the project-cleaner executor.
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cleanup_helpers import QUARANTINE_SCHEMA_VERSION, _parse_iso


class CleanupQuarantineOpsMixin:

        def purge_quarantine(
            self,
            older_than_hours: int | None = None,
            *,
            permanent: bool = False,
        ) -> dict[str, Any]:
            with self._mutation_guard("purge") as lock:
                if not lock.get("acquired"):
                    return {
                        "ok": False,
                        "busy": True,
                        "purged_dirs": 0,
                        "purged_bytes": 0,
                        "permanently_deleted": 0,
                        "errors": [],
                        "skipped": [],
                        "message": str(lock.get("message") or "project cleaner is busy"),
                    }
                return self._purge_quarantine_unlocked(
                    older_than_hours,
                    permanent=permanent,
                )

        def _purge_quarantine_unlocked(
            self,
            older_than_hours: int | None = None,
            *,
            permanent: bool = False,
        ) -> dict[str, Any]:
            ttl = max(1, int(older_than_hours or self._quarantine_ttl_hours()))
            purged_dirs = 0
            purged_bytes = 0
            errors: list[dict[str, str]] = []
            skipped: list[dict[str, str]] = []
            archives: list[dict[str, Any]] = []
            if not self.quarantine_root.exists():
                return self._purge_empty_result()
            now = datetime.now(timezone.utc)
            for child in sorted(self.quarantine_root.iterdir(), key=lambda item: item.name):
                if self._is_link_or_reparse_point(child) or not child.is_dir():
                    continue
                document, error = self._read_batch_document(child)
                if document is None:
                    skipped.append({"path": child.name, "reason": error})
                    continue
                block_reason = self._purge_batch_blocked(document, child, now, ttl, permanent)
                if block_reason is not None:
                    if block_reason:
                        skipped.append({"path": child.name, "reason": block_reason})
                    continue
                try:
                    purged = self._purge_one_batch(
                        child, document, permanent, skipped, archives
                    )
                except OSError as exc:
                    errors.append({"path": str(child), "message": str(exc)})
                    continue
                purged_dirs += purged[0]
                purged_bytes += purged[1]
            return self._purge_quarantine_result(
                purged_dirs, purged_bytes, permanent, archives, errors, skipped
            )

        def restore_quarantine(
            self, batch_name: str, *, conflict_strategy: str = "skip"
        ) -> dict[str, Any]:
            clean_name = Path(str(batch_name or "").strip()).name
            if not clean_name:
                return {"ok": False, "message": "quarantine batch is required"}
            with self._mutation_guard("restore", batch_name=clean_name) as lock:
                if not lock.get("acquired"):
                    return {
                        "ok": False,
                        "busy": True,
                        "restored": 0,
                        "renamed": 0,
                        "skipped": [],
                        "errors": [],
                        "message": str(lock.get("message") or "project cleaner is busy"),
                    }
                return self._restore_quarantine_unlocked(
                    clean_name,
                    conflict_strategy=conflict_strategy,
                )

        def _restore_quarantine_unlocked(
            self, batch_name: str, *, conflict_strategy: str = "skip"
        ) -> dict[str, Any]:
            batch_dir, document, failure, clean_name = self._select_restore_batch(batch_name)
            if failure is not None:
                return failure
            strategy = conflict_strategy if conflict_strategy in {"skip", "rename"} else "skip"
            restored = 0
            renamed = 0
            skipped: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            items = [item for item in document.get("items", []) if isinstance(item, dict)]
            for index, item in enumerate(items):
                restored_delta, renamed_delta = self._restore_quarantine_item(
                    item, batch_dir, document, items, strategy, skipped, errors
                )
                restored += restored_delta
                renamed += renamed_delta
            if not any(item.get("status") == "moved" for item in items):
                document["status"] = "restored"
            document["items"] = items
            document["restore_errors"] = errors
            document["restore_skips"] = skipped
            self._write_batch_document(batch_dir, document)
            result = {
                "ok": not errors,
                "restored": restored,
                "renamed": renamed,
                "conflict_strategy": strategy,
                "skipped": skipped,
                "errors": errors,
                "message": f"quarantine restore completed (items={restored})",
            }
            self._append_history("restore", ok=result["ok"], batch_id=clean_name, item_count=restored, renamed=renamed, errors=len(errors))
            return result

        def _purge_batch_blocked(
            self,
            document: dict[str, Any],
            child: Path,
            now: datetime,
            ttl: int,
            permanent: bool,
        ) -> str | None:
            recoverable = any(
                isinstance(item, dict)
                and item.get("status") in {"pending", "error"}
                and item.get("quarantine_path")
                for item in document.get("items", [])
            )
            if document.get("pinned") or str(document.get("status")) == "applying" or recoverable:
                return "pinned or recoverable batch"
            created = _parse_iso(str(document.get("created_at") or "")) or now
            configured_expiration = self._batch_expiration(document, child)
            requested_expiration = created + timedelta(hours=ttl)
            if not permanent and now < min(
                configured_expiration,
                requested_expiration,
            ):
                return ""
            return None

        def _batch_integrity_errors(
            self,
            child: Path,
            document: dict[str, Any],
        ) -> tuple[int, list[str]]:
            size = sum(
                int(item.get("size_bytes") or 0)
                for item in document.get("items", [])
                if isinstance(item, dict)
            )
            integrity_errors: list[str] = []
            for item in document.get("items", []):
                if not isinstance(item, dict) or not self._is_restorable_item(item):
                    continue
                relative = str(item.get("quarantine_path") or "").strip()
                source = (child / relative).resolve()
                try:
                    source.relative_to(child.resolve())
                except ValueError:
                    integrity_errors.append(f"{relative}: escaped batch")
                    continue
                if not source.exists() or self._is_link_or_reparse_point(source):
                    integrity_errors.append(f"{relative}: missing or unsafe")
                    continue
                expected = str(item.get("content_sha256") or "")
                actual = self._content_digest(
                    source,
                    str(item.get("type") or "file"),
                )
                if not expected or actual != expected:
                    integrity_errors.append(f"{relative}: SHA-256 mismatch")
            return size, integrity_errors

        def _archive_purged_batch(
            self,
            child: Path,
            document: dict[str, Any],
        ) -> tuple[str | None, dict[str, Any] | None]:
            archive_root = self.recovery_root / "purged"
            archive_root.mkdir(parents=True, exist_ok=True)
            self._harden_private_path(self.recovery_root)
            self._harden_private_path(archive_root)
            archive_dir = archive_root / child.name
            if archive_dir.exists():
                return "recovery archive destination already exists", None
            os.replace(child, archive_dir)
            document["status"] = "archived"
            document["archived_at"] = self._iso_now()
            document["archive_path"] = str(archive_dir)
            self._write_batch_document(archive_dir, document)
            manifest_document = (
                archive_dir / "manifest.json"
                if (archive_dir / "manifest.json").exists()
                else archive_dir / "journal.json"
            )
            manifest_sha256 = self._file_sha256(manifest_document)
            purge_tombstone = {
                "schema_version": QUARANTINE_SCHEMA_VERSION,
                "kind": "purge-tombstone",
                "status": "recoverable",
                "created_at": self._iso_now(),
                "batch_id": child.name,
                "original_path": str(child),
                "recovery_path": str(archive_dir),
                "manifest_path": str(manifest_document),
                "manifest_sha256": manifest_sha256,
                "permanently_deleted": 0,
            }
            self._atomic_write_json(
                archive_dir / "purge-tombstone.json",
                purge_tombstone,
            )
            return None, purge_tombstone

        def _purge_one_batch(
            self,
            child: Path,
            document: dict[str, Any],
            permanent: bool,
            skipped: list[dict[str, str]],
            archives: list[dict[str, Any]],
        ) -> tuple[int, int]:
            size, integrity_errors = self._batch_integrity_errors(child, document)
            if integrity_errors:
                skipped.append(
                    {
                        "path": child.name,
                        "reason": "; ".join(integrity_errors),
                    }
                )
                return 0, 0
            if permanent:
                locked_path = self._locked_descendant(child)
                if locked_path is not None:
                    skipped.append(
                        {
                            "path": child.name,
                            "reason": "batch contains an in-use file",
                        }
                    )
                    return 0, 0
                shutil.rmtree(child)
                return 1, size
            archive_failure, tombstone = self._archive_purged_batch(child, document)
            if archive_failure is not None:
                skipped.append({"path": child.name, "reason": archive_failure})
                return 0, 0
            archives.append(tombstone)
            return 1, size

        def _purge_empty_result(self) -> dict[str, Any]:
            return {
                "ok": True,
                "purged_dirs": 0,
                "purged_bytes": 0,
                "archived_dirs": 0,
                "archived_bytes": 0,
                "archives": [],
                "permanently_deleted": 0,
                "errors": [],
                "skipped": [],
                "message": "quarantine is empty",
            }

        def _purge_quarantine_result(
            self,
            purged_dirs: int,
            purged_bytes: int,
            permanent: bool,
            archives: list[dict[str, Any]],
            errors: list[dict[str, str]],
            skipped: list[dict[str, str]],
        ) -> dict[str, Any]:
            result = {
                "ok": not errors,
                "purged_dirs": purged_dirs,
                "purged_bytes": purged_bytes,
                "archived_dirs": purged_dirs,
                "archived_bytes": purged_bytes,
                "disk_space_reclaimed_bytes": purged_bytes if permanent else 0,
                "archives": archives,
                "permanently_deleted": purged_dirs if permanent else 0,
                "errors": errors,
                "skipped": skipped,
                "message": (
                    f"quarantine permanently deleted (dirs={purged_dirs})"
                    if permanent
                    else f"quarantine archival completed (dirs={purged_dirs})"
                ),
            }
            self._append_history(
                "purge",
                ok=result["ok"],
                item_count=purged_dirs,
                bytes=purged_bytes,
                errors=len(errors),
            )
            return result

        def _select_restore_batch(
            self, batch_name: str
        ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, str]:
            clean_name = Path(str(batch_name or "").strip()).name
            live_batch = self.quarantine_root / clean_name
            archived_batch = self.recovery_root / "purged" / clean_name
            selected_root = self.quarantine_root
            selected_batch = live_batch
            if not live_batch.exists() and archived_batch.exists():
                selected_root = self.recovery_root / "purged"
                selected_batch = archived_batch
            batch_dir = selected_batch.resolve()
            try:
                batch_dir.relative_to(selected_root.resolve())
            except ValueError:
                return None, None, {"ok": False, "message": "invalid quarantine batch"}, clean_name
            document, error = self._read_batch_document(batch_dir)
            if document is None:
                return None, None, {"ok": False, "message": error}, clean_name
            return batch_dir, document, None, clean_name

        def _restore_quarantine_item(
            self,
            item: dict[str, Any],
            batch_dir: Path,
            document: dict[str, Any],
            items: list[dict[str, Any]],
            strategy: str,
            skipped: list[dict[str, Any]],
            errors: list[dict[str, Any]],
        ) -> tuple[int, int]:
            precheck = self._restore_item_precheck(
                item, batch_dir, skipped, errors
            )
            if precheck[0] is None:
                return 0, 0
            source, destination, original_rel = precheck
            if item.get("contents_only") is True:
                return self._restore_contents_only_item(
                    item, source, destination, original_rel,
                    batch_dir, document, items, strategy, skipped, errors,
                )
            if destination.exists():
                if strategy == "rename":
                    destination = self._unique_restore_destination(destination)
                    renamed = 1
                else:
                    skipped.append({"path": original_rel, "reason": "destination already exists"})
                    return 0, 0
            else:
                renamed = 0
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                item["status"] = "restored"
                item["restored_path"] = destination.relative_to(self.project_root).as_posix()
                document["items"] = items
                self._write_batch_document(batch_dir, document)
            except OSError as exc:
                errors.append({"path": original_rel, "message": str(exc), "locked_by": self._locking_processes(destination)})
                return 0, 0
            return 1, renamed

        def _restore_contents_only_item(
            self,
            item: dict[str, Any],
            source: Path,
            destination: Path,
            original_rel: str,
            batch_dir: Path,
            document: dict[str, Any],
            items: list[dict[str, Any]],
            strategy: str,
            skipped: list[dict[str, Any]],
            errors: list[dict[str, Any]],
        ) -> tuple[int, int]:
            if destination.exists() and not destination.is_dir():
                skipped.append(
                    {"path": original_rel, "reason": "destination is not a directory"}
                )
                return 0, 0
            try:
                destination.mkdir(parents=True, exist_ok=True)
                source_files = self._regular_files_below(source)
                restored, renamed, item_skipped = self._restore_content_files(
                    source_files, source, destination, original_rel,
                    strategy, skipped,
                )
                if not item_skipped:
                    item["status"] = "restored"
                    item["restored_path"] = original_rel
                document["items"] = items
                self._write_batch_document(batch_dir, document)
            except OSError as exc:
                errors.append(
                    {
                        "path": original_rel,
                        "message": str(exc),
                        "locked_by": self._locking_processes(destination),
                    }
                )
                return 0, 0
            return restored, renamed

        def _restore_item_precheck(
            self,
            item: dict[str, Any],
            batch_dir: Path,
            skipped: list[dict[str, Any]],
            errors: list[dict[str, Any]],
        ) -> tuple[Path | None, Path | None, str | None]:
            if not self._is_restorable_item(item):
                return None, None, None
            original_rel = str(item.get("path") or item.get("original_path") or "").strip()
            quarantine_rel = str(item.get("quarantine_path") or "").strip()
            if not original_rel or not quarantine_rel:
                return None, None, None
            source = (batch_dir / quarantine_rel).resolve()
            destination = (self.project_root / original_rel).resolve()
            try:
                source.relative_to(batch_dir)
                destination.relative_to(self.project_root)
            except ValueError:
                skipped.append({"path": original_rel, "reason": "invalid restore path"})
                return None, None, None
            if not source.exists() or self._is_link_or_reparse_point(source):
                skipped.append({"path": original_rel, "reason": "quarantine item missing or unsafe"})
                return None, None, None
            expected_digest = str(item.get("content_sha256") or "")
            if expected_digest:
                try:
                    actual_digest = self._content_digest(source, str(item.get("type") or "file"))
                except OSError as exc:
                    errors.append({"path": original_rel, "message": str(exc)})
                    return None, None, None
                if actual_digest != expected_digest:
                    skipped.append({"path": original_rel, "reason": "quarantine integrity mismatch"})
                    return None, None, None
            return source, destination, original_rel

        def _restore_content_files(
            self,
            source_files: list[Path],
            source: Path,
            destination: Path,
            original_rel: str,
            strategy: str,
            skipped: list[dict[str, Any]],
        ) -> tuple[int, int, bool]:
            restored = 0
            renamed = 0
            item_skipped = False
            for source_file in source_files:
                relative_file = source_file.relative_to(source)
                destination_file = destination / relative_file
                if destination_file.exists():
                    if strategy == "rename":
                        destination_file = self._unique_restore_destination(
                            destination_file
                        )
                        renamed += 1
                    else:
                        skipped.append(
                            {
                                "path": (
                                    Path(original_rel) / relative_file
                                ).as_posix(),
                                "reason": "destination already exists",
                            }
                        )
                        item_skipped = True
                        continue
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_file), str(destination_file))
                restored += 1
            return restored, renamed, item_skipped
