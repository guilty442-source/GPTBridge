from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cleanup_constants import QUARANTINE_SCHEMA_VERSION, _parse_iso


class QuarantineMixin:
    """Quarantine batch listing, pinning, purging, and restoration."""

    def _batch_expiration(self, batch: dict[str, Any], batch_dir: Path) -> datetime:
        expires = _parse_iso(str(batch.get("expires_at") or ""))
        if expires is not None:
            return expires
        try:
            modified = datetime.fromtimestamp(batch_dir.stat(follow_symlinks=False).st_mtime, timezone.utc)
        except OSError:
            modified = datetime.now(timezone.utc)
        return modified + timedelta(hours=self._quarantine_ttl_hours())

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
            if not batch_root.exists():
                continue
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
        now = datetime.now(timezone.utc)
        for child in sorted(self.quarantine_root.iterdir(), key=lambda item: item.name):
            if self._is_link_or_reparse_point(child) or not child.is_dir():
                continue
            document, error = self._read_batch_document(child)
            if document is None:
                skipped.append({"path": child.name, "reason": error})
                continue
            recoverable = any(
                isinstance(item, dict)
                and item.get("status") in {"pending", "error"}
                and item.get("quarantine_path")
                for item in document.get("items", [])
            )
            if document.get("pinned") or str(document.get("status")) == "applying" or recoverable:
                skipped.append({"path": child.name, "reason": "pinned or recoverable batch"})
                continue
            created = _parse_iso(str(document.get("created_at") or "")) or now
            configured_expiration = self._batch_expiration(document, child)
            requested_expiration = created + timedelta(hours=ttl)
            if not permanent and now < min(
                configured_expiration,
                requested_expiration,
            ):
                continue
            try:
                size = sum(int(item.get("size_bytes") or 0) for item in document.get("items", []) if isinstance(item, dict))
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
                if integrity_errors:
                    skipped.append(
                        {
                            "path": child.name,
                            "reason": "; ".join(integrity_errors),
                        }
                    )
                    continue

                if permanent:
                    locked_path = self._locked_descendant(child)
                    if locked_path is not None:
                        skipped.append(
                            {
                                "path": child.name,
                                "reason": "batch contains an in-use file",
                            }
                        )
                        continue
                    shutil.rmtree(child)
                    purged_dirs += 1
                    purged_bytes += size
                    continue

                archive_root = self.recovery_root / "purged"
                archive_root.mkdir(parents=True, exist_ok=True)
                self._harden_private_path(self.recovery_root)
                self._harden_private_path(archive_root)
                archive_dir = archive_root / child.name
                if archive_dir.exists():
                    skipped.append(
                        {
                            "path": child.name,
                            "reason": "recovery archive destination already exists",
                        }
                    )
                    continue
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
                purged_dirs += 1
                purged_bytes += size
                archives.append(purge_tombstone)
            except OSError as exc:
                errors.append({"path": str(child), "message": str(exc)})
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
        self._append_history("purge", ok=result["ok"], item_count=purged_dirs, bytes=purged_bytes, errors=len(errors))
        return result

    @staticmethod
    def _unique_restore_destination(destination: Path) -> Path:
        for index in range(1, 1000):
            candidate = destination.with_name(f"{destination.stem}.restored-{index}{destination.suffix}")
            if not candidate.exists():
                return candidate
        return destination.with_name(f"{destination.stem}.restored-{uuid.uuid4().hex[:8]}{destination.suffix}")

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
            return {"ok": False, "message": "invalid quarantine batch"}
        document, error = self._read_batch_document(batch_dir)
        if document is None:
            return {"ok": False, "message": error}
        strategy = conflict_strategy if conflict_strategy in {"skip", "rename"} else "skip"
        restored = 0
        renamed = 0
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        items = [item for item in document.get("items", []) if isinstance(item, dict)]
        for index, item in enumerate(items):
            if not self._is_restorable_item(item):
                continue
            original_rel = str(item.get("path") or item.get("original_path") or "").strip()
            quarantine_rel = str(item.get("quarantine_path") or "").strip()
            if not original_rel or not quarantine_rel:
                continue
            source = (batch_dir / quarantine_rel).resolve()
            destination = (self.project_root / original_rel).resolve()
            try:
                source.relative_to(batch_dir)
                destination.relative_to(self.project_root)
            except ValueError:
                skipped.append({"path": original_rel, "reason": "invalid restore path"})
                continue
            if not source.exists() or self._is_link_or_reparse_point(source):
                skipped.append({"path": original_rel, "reason": "quarantine item missing or unsafe"})
                continue
            expected_digest = str(item.get("content_sha256") or "")
            if expected_digest:
                try:
                    actual_digest = self._content_digest(source, str(item.get("type") or "file"))
                except OSError as exc:
                    errors.append({"path": original_rel, "message": str(exc)})
                    continue
                if actual_digest != expected_digest:
                    skipped.append({"path": original_rel, "reason": "quarantine integrity mismatch"})
                    continue
            if item.get("contents_only") is True:
                if destination.exists() and not destination.is_dir():
                    skipped.append(
                        {
                            "path": original_rel,
                            "reason": "destination is not a directory",
                        }
                    )
                    continue
                try:
                    destination.mkdir(parents=True, exist_ok=True)
                    source_files = self._regular_files_below(source)
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
                continue
            if destination.exists():
                if strategy == "rename":
                    destination = self._unique_restore_destination(destination)
                    renamed += 1
                else:
                    skipped.append({"path": original_rel, "reason": "destination already exists"})
                    continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                item["status"] = "restored"
                item["restored_path"] = destination.relative_to(self.project_root).as_posix()
                restored += 1
                document["items"] = items
                self._write_batch_document(batch_dir, document)
            except OSError as exc:
                errors.append({"path": original_rel, "message": str(exc), "locked_by": self._locking_processes(destination)})
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
