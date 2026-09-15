# Repair apply pipeline mixin for the project-cleaner executor.
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cleanup_helpers import QUARANTINE_SCHEMA_VERSION


class CleanupRepairApplyMixin:

            def _apply_anomaly_repair_locked(
                self,
                base_result: dict[str, Any],
                candidates: list[dict[str, Any]],
                include_shared: bool,
            ) -> dict[str, Any]:
                batch_name, batch_dir, document = self._new_repair_batch(
                    candidates
                )
                entries = document["items"]
                repaired_count, repaired_bytes = self._apply_repair_moves(
                    candidates, document, batch_dir
                )
                errors = document["errors"]
                skipped = document["skipped"]
                document["status"] = (
                    "completed_with_errors" if errors else "completed"
                )
                self._write_batch_document(batch_dir, document)
                manifest_path = batch_dir / "manifest.json"
                if manifest_path.is_file():
                    self._write_repair_tombstone(
                        batch_dir, batch_name, manifest_path, entries
                    )
                return self._finish_anomaly_repair(
                    base_result,
                    entries,
                    repaired_count,
                    repaired_bytes,
                    errors,
                    skipped,
                    batch_dir,
                    manifest_path,
                    batch_name,
                    include_shared,
                )

            def _new_repair_batch(
                self, candidates: list[dict[str, Any]]
            ) -> tuple[str, Path, dict[str, Any]]:
                created_at = datetime.now(timezone.utc)
                batch_name = (
                    f"repair-{created_at.strftime('%Y%m%d_%H%M%S_%f')}-"
                    f"{uuid.uuid4().hex[:8]}"
                )
                batch_dir = self.quarantine_root / batch_name
                batch_dir.mkdir(parents=True, exist_ok=False)
                self._harden_private_path(batch_dir)
                document: dict[str, Any] = {
                    "schema_version": QUARANTINE_SCHEMA_VERSION,
                    "batch_id": batch_name,
                    "status": "applying",
                    "created_at": created_at.isoformat(),
                    "expires_at": (
                        created_at + timedelta(hours=self._quarantine_ttl_hours())
                    ).isoformat(),
                    "pinned": False,
                    "project_root": str(self.project_root),
                    "scope": "anomaly-repair",
                    "plan_id": "",
                    "items": [
                        {
                            **item,
                            "original_path": item["path"],
                            "quarantine_path": (
                                Path("items") / str(item["path"])
                            ).as_posix(),
                            "content_sha256": "",
                            "status": "pending",
                        }
                        for item in candidates
                    ],
                    "errors": [],
                    "skipped": [],
                }
                self._write_batch_document(batch_dir, document)
                return batch_name, batch_dir, document

            def _apply_repair_moves(
                self,
                candidates: list[dict[str, Any]],
                document: dict[str, Any],
                batch_dir: Path,
            ) -> tuple[int, int]:
                repaired_count = 0
                repaired_bytes = 0
                for index, item in enumerate(candidates):
                    count, moved_bytes = self._repair_one_item(
                        item, index, document, batch_dir, len(candidates)
                    )
                    repaired_count += count
                    repaired_bytes += moved_bytes
                    self._write_batch_document(batch_dir, document)
                return repaired_count, repaired_bytes

            def _repair_one_item(
                self,
                item: dict[str, Any],
                index: int,
                document: dict[str, Any],
                batch_dir: Path,
                total: int,
            ) -> tuple[int, int]:
                relative_path = str(item["path"])
                target = self.project_root / relative_path
                entries = document["items"]
                recovery_target = (
                    batch_dir / str(entries[index]["quarantine_path"])
                )
                self._emit_progress(
                    "repair",
                    5 + int(index / max(1, total) * 90),
                    "Repairing recoverable project anomaly",
                    current_path=relative_path,
                    completed=index,
                    total=total,
                )
                decision, payload = self._check_repair_target(
                    target, relative_path, item
                )
                if decision is not None:
                    if decision == "locked":
                        entries[index]["locked_by"] = payload["locked_by"]
                    self._mark_repair_entry(
                        document, index, decision, payload
                    )
                    return 0, 0
                try:
                    recovery_target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, recovery_target)
                    entries[index]["content_sha256"] = self._content_digest(
                        recovery_target, str(item["type"])
                    )
                    entries[index]["status"] = "moved"
                except OSError as exc:
                    self._mark_repair_entry(
                        document, index, "error",
                        {"path": relative_path, "message": str(exc)},
                    )
                    return 0, 0
                return 1, int(item.get("size_bytes") or 0)

            def _check_repair_target(
                self,
                target: Path,
                relative_path: str,
                item: dict[str, Any],
            ) -> tuple[str | None, dict[str, Any] | None]:
                if (
                    not target.exists()
                    or self._is_link_or_reparse_point(target)
                ):
                    return "skipped", {
                        "path": relative_path,
                        "reason": "anomaly changed before repair",
                    }
                try:
                    current_snapshot = self._candidate_snapshot(
                        target, str(item["type"])
                    )
                except OSError as exc:
                    return "error", {
                        "path": relative_path,
                        "message": str(exc),
                    }
                expected = item.get("fingerprint", {})
                if current_snapshot.get("digest") != expected.get("digest"):
                    return "skipped", {
                        "path": relative_path,
                        "reason": "anomaly changed after scan",
                    }
                if self._is_locked(target):
                    return "locked", {
                        "path": relative_path,
                        "reason": (
                            "path is in use; cleaner never forces unlock"
                        ),
                        "locked_by": self._locking_processes(target),
                    }
                return None, None

            @staticmethod
            def _mark_repair_entry(
                document: dict[str, Any],
                index: int,
                status: str,
                detail: dict[str, Any],
            ) -> None:
                entries = document["items"]
                entries[index]["status"] = status
                if status == "error":
                    entries[index]["status_reason"] = detail["message"]
                    document["errors"].append(detail)
                else:
                    if detail.get("reason"):
                        entries[index]["status_reason"] = detail["reason"]
                    document["skipped"].append(detail)

            def _write_repair_tombstone(
                self,
                batch_dir: Path,
                batch_name: str,
                manifest_path: Path,
                entries: list[dict[str, Any]],
            ) -> None:
                self._atomic_write_json(
                    batch_dir / "tombstone.json",
                    {
                        "schema_version": QUARANTINE_SCHEMA_VERSION,
                        "kind": "repair-tombstone",
                        "status": "recoverable",
                        "created_at": self._iso_now(),
                        "batch_id": batch_name,
                        "scope": "anomaly-repair",
                        "manifest_path": str(manifest_path),
                        "manifest_sha256": self._file_sha256(manifest_path),
                        "items": [
                            {
                                "original_path": str(
                                    item.get("original_path") or ""
                                ),
                                "recovery_path": str(
                                    item.get("quarantine_path") or ""
                                ),
                                "content_sha256": str(
                                    item.get("content_sha256") or ""
                                ),
                                "type": str(item.get("type") or ""),
                            }
                            for item in entries
                            if str(item.get("status") or "") == "moved"
                        ],
                    },
                )
