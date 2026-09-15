"""Packager distribution helpers (A185 split).

Contains the validation/inventory and rollback helpers extracted from
promote_staged_distribution.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packager_base import (
    PACKAGE_METADATA_NAME,
    PromotionRecoveryRequired,
)
from packager_inventory import (
    _inventory_package_tree,
    _is_link_or_reparse,
    _persist_package_document,
)


def _validate_staged_distribution(staged_dist: Path) -> Path:
    """Validate the staged distribution is safe and return abspath."""
    staged_dist = Path(os.path.abspath(staged_dist))
    if (
        not staged_dist.exists()
        or _is_link_or_reparse(staged_dist)
        or staged_dist.resolve(strict=True) != staged_dist
    ):
        raise RuntimeError(f"Staged distribution is unsafe: {staged_dist}")
    return staged_dist


def _collect_live_distribution_info(
    dist_dir: Path,
    staged_inventory: dict[str, Any],
) -> dict[str, Any]:
    """Collect information about the live distribution.

    Returns a dict with:
      - had_live_dist
      - previous_inventory
      - previous_metadata
      - unknown_live_app_paths
      - unknown_live_runtime_paths
    """
    had_live_dist = dist_dir.exists() or dist_dir.is_symlink()
    previous_inventory: dict[str, Any] | None = None
    previous_metadata: dict[str, Any] = {}
    unknown_live_app_paths: list[str] = []
    unknown_live_runtime_paths: list[str] = []
    if had_live_dist:
        if (
            _is_link_or_reparse(dist_dir)
            or not dist_dir.is_dir()
            or dist_dir.resolve(strict=True) != dist_dir
        ):
            raise RuntimeError(f"Live distribution is unsafe: {dist_dir}")
        previous_inventory = _inventory_package_tree(dist_dir)
        live_app = dist_dir / "resources" / "app"
        metadata_path = live_app / PACKAGE_METADATA_NAME
        if metadata_path.is_file() and not _is_link_or_reparse(metadata_path):
            try:
                previous_metadata = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                previous_metadata = {}
        owned_payload_paths = {
            f"resources/app/{relative_path}"
            for relative_path in (
                previous_metadata.get("payload_files")
                if isinstance(previous_metadata.get("payload_files"), dict)
                else {}
            )
        }
        previous_file_paths = {
            str(entry["path"])
            for entry in previous_inventory["entries"]
            if entry.get("type") == "file"
        }
        unknown_live_app_paths = sorted(
            relative_path
            for relative_path in previous_file_paths
            if relative_path.startswith("resources/app/")
            and relative_path not in owned_payload_paths
            and relative_path
            != f"resources/app/{PACKAGE_METADATA_NAME}"
        )
        staged_file_paths = {
            str(entry["path"])
            for entry in staged_inventory["entries"]
            if entry.get("type") == "file"
        }
        unknown_live_runtime_paths = sorted(
            relative_path
            for relative_path in previous_file_paths
            if not relative_path.startswith("resources/app/")
            and relative_path not in staged_file_paths
        )
    return {
        "had_live_dist": had_live_dist,
        "previous_inventory": previous_inventory,
        "previous_metadata": previous_metadata,
        "unknown_live_app_paths": unknown_live_app_paths,
        "unknown_live_runtime_paths": unknown_live_runtime_paths,
    }


def _persist_promotion_journal(
    journal_path: Path,
    package_root: Path,
    staged_dist: Path,
    dist_dir: Path,
    previous_dist: Path,
    staged_inventory: dict[str, Any],
    previous_inventory: dict[str, Any] | None,
    unknown_live_app_paths: list[str],
    unknown_live_runtime_paths: list[str],
) -> None:
    """Persist the promotion journal with prepared status."""
    _persist_package_document(
        journal_path,
        {
            "format_version": 1,
            "status": "prepared",
            "operation_id": package_root.name,
            "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
            "staged_dist": str(staged_dist),
            "live_dist": str(dist_dir),
            "previous_dist": str(previous_dist),
            "staged_tree_digest": staged_inventory["tree_digest"],
            "previous_tree_digest": (
                previous_inventory["tree_digest"]
                if previous_inventory is not None
                else ""
            ),
            "unknown_live_app_paths": unknown_live_app_paths,
            "unknown_live_runtime_paths": unknown_live_runtime_paths,
        },
    )


def _handle_promotion_rollback(
    error: BaseException,
    installed_new: bool,
    moved_previous: bool,
    dist_dir: Path,
    failed_dist: Path,
    previous_dist: Path,
    package_root: Path,
) -> None:
    """Handle rollback after a promotion failure.

    Raises PromotionRecoveryRequired if rollback is incomplete.
    """
    rollback_errors: list[str] = []
    if installed_new and dist_dir.exists():
        try:
            if failed_dist.exists() or failed_dist.is_symlink():
                failed_dist = package_root / (
                    f"failed-new-dist-{uuid.uuid4().hex}"
                )
            os.replace(dist_dir, failed_dist)
        except BaseException as rollback_error:
            rollback_errors.append(
                f"preserve failed new distribution: {rollback_error}"
            )
    if moved_previous:
        try:
            if previous_dist.exists() and not dist_dir.exists():
                os.replace(previous_dist, dist_dir)
            elif not dist_dir.exists():
                raise FileNotFoundError(
                    f"Previous distribution is missing: {previous_dist}"
                )
        except BaseException as rollback_error:
            rollback_errors.append(
                f"restore previous distribution: {rollback_error}"
            )
    try:
        _persist_package_document(
            package_root / "promotion-aborted.json",
            {
                "format_version": 1,
                "status": (
                    "rollback-incomplete"
                    if rollback_errors
                    else "aborted-and-rolled-back"
                ),
                "aborted_at_utc": datetime.now(timezone.utc).isoformat(),
                "error": str(error),
                "rollback_errors": rollback_errors,
                "live_dist": str(dist_dir),
                "previous_dist": str(previous_dist),
                "failed_new_dist": (
                    str(failed_dist) if failed_dist.exists() else ""
                ),
            },
        )
    except BaseException as journal_error:
        rollback_errors.append(f"persist abort journal: {journal_error}")
    if rollback_errors:
        raise PromotionRecoveryRequired(
            "Distribution update failed and recovery is incomplete: "
            + "; ".join(rollback_errors),
            recovery_root=package_root,
            rollback_errors=rollback_errors,
        ) from error
    raise


__all__ = [
    "_validate_staged_distribution",
    "_collect_live_distribution_info",
    "_persist_promotion_journal",
    "_handle_promotion_rollback",
]
