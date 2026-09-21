from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Sequence

from packager_base import (
    PACKAGE_METADATA_NAME,
    PromotionRecoveryRequired,
)
from packager_inventory import (
    _inventory_file_map,
    _inventory_package_tree,
    _is_link_or_reparse,
    _persist_package_document,
    _sha256_regular_file,
)
from packager_distribution_helpers import (
    _validate_staged_distribution,
    _collect_live_distribution_info,
    _persist_promotion_journal,
    _handle_promotion_rollback,
)


def _synchronize_distribution_files_in_place(
    source_root: Path,
    live_root: Path,
    *,
    retired_root: Path,
    preserve_paths: Collection[str] = (),
) -> dict[str, Any]:
    """Replace files without renaming the watched live distribution root.

    ``preserve_paths`` names live-only files (payload-relative, forward
    slashes) that must stay in place — runtime state the update must not
    overwrite (settings/data/weights/sessions/unfinished tasks).
    """

    source_inventory = _inventory_package_tree(source_root)
    live_inventory = _inventory_package_tree(live_root)
    source_files = _inventory_file_map(source_inventory)
    live_files = _inventory_file_map(live_inventory)
    retired_root.mkdir(parents=True, exist_ok=False)
    preserved = set(preserve_paths)

    for relative_path in sorted(set(live_files) - set(source_files)):
        if relative_path in preserved:
            continue
        live_path = live_root / Path(*relative_path.split("/"))
        retired_path = retired_root / "removed-from-live" / Path(
            *relative_path.split("/")
        )
        retired_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(live_path, retired_path)

    source_directories = sorted(
        (
            str(entry["path"])
            for entry in source_inventory["entries"]
            if entry.get("type") == "directory"
        ),
        key=lambda value: (value.count("/"), value),
    )
    for relative_path in source_directories:
        target = live_root / Path(*relative_path.split("/"))
        if target.exists() and not target.is_dir():
            conflict = retired_root / "type-conflicts" / Path(
                *relative_path.split("/")
            )
            conflict.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, conflict)
        target.mkdir(parents=True, exist_ok=True)

    for relative_path, expected in sorted(source_files.items()):
        source = source_root / Path(*relative_path.split("/"))
        target = live_root / Path(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_dir():
            conflict = (
                retired_root
                / "type-conflicts"
                / Path(*relative_path.split("/"))
            )
            conflict.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, conflict)
        if target.is_file() and not _is_link_or_reparse(target):
            current = (int(target.stat().st_size), _sha256_regular_file(target))
            if current == expected:
                continue

        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
        )
        try:
            shutil.copy2(source, temporary, follow_symlinks=False)
            copied = (
                int(temporary.stat().st_size),
                _sha256_regular_file(temporary),
            )
            if copied != expected:
                raise RuntimeError(
                    f"In-place package copy verification failed: {relative_path}"
                )
            if target.exists() or target.is_symlink():
                retired = retired_root / "replaced-live" / Path(
                    *relative_path.split("/")
                )
                retired.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, retired)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    installed_inventory = _inventory_package_tree(live_root)
    installed_files = _inventory_file_map(installed_inventory)
    if installed_files != source_files:
        raise RuntimeError(
            "In-place installed distribution files do not match staged package"
        )
    if _inventory_package_tree(source_root) != source_inventory:
        raise RuntimeError("Staged distribution changed during in-place promotion")
    return installed_inventory


def _promote_staged_distribution_in_place(
    *,
    staged_dist: Path,
    dist_dir: Path,
    package_root: Path,
    previous_dist: Path,
    previous_inventory: dict[str, Any],
    previous_metadata: dict[str, Any],
    unknown_live_app_paths: list[str],
    unknown_live_runtime_paths: list[str],
    root_rename_error: PermissionError,
) -> Path:
    """Windows fallback for directories held by a non-destructive watcher."""

    if previous_dist.exists() or previous_dist.is_symlink():
        raise RuntimeError(
            f"Previous distribution recovery already exists: {previous_dist}"
        )
    shutil.copytree(dist_dir, previous_dist, copy_function=shutil.copy2)
    retained_inventory = _inventory_package_tree(previous_dist)
    current_inventory = _inventory_package_tree(dist_dir)
    if (
        retained_inventory != previous_inventory
        or current_inventory != previous_inventory
    ):
        raise RuntimeError(
            "Previous distribution changed during verified fallback copy"
        )

    retired_root = package_root / "retired-live-paths"
    failed_live = package_root / "failed-live-dist"
    try:
        installed_inventory = _synchronize_distribution_files_in_place(
            staged_dist,
            dist_dir,
            retired_root=retired_root,
            preserve_paths=unknown_live_runtime_paths,
        )
    except BaseException as install_error:
        rollback_errors: list[str] = []
        try:
            shutil.copytree(dist_dir, failed_live, copy_function=shutil.copy2)
            _inventory_package_tree(failed_live)
        except BaseException as capture_error:
            rollback_errors.append(
                f"capture failed in-place distribution: {capture_error}"
            )
        try:
            _synchronize_distribution_files_in_place(
                previous_dist,
                dist_dir,
                retired_root=package_root
                / f"rollback-retired-{uuid.uuid4().hex}",
            )
            if _inventory_file_map(_inventory_package_tree(dist_dir)) != (
                _inventory_file_map(previous_inventory)
            ):
                raise RuntimeError("in-place rollback verification mismatch")
        except BaseException as rollback_error:
            rollback_errors.append(
                f"restore previous distribution in place: {rollback_error}"
            )
        _persist_package_document(
            package_root / "promotion-aborted.json",
            {
                "format_version": 1,
                "status": (
                    "rollback-incomplete"
                    if rollback_errors
                    else "aborted-and-rolled-back"
                ),
                "strategy": "verified-in-place",
                "aborted_at_utc": datetime.now(timezone.utc).isoformat(),
                "root_rename_error": str(root_rename_error),
                "error": str(install_error),
                "rollback_errors": rollback_errors,
                "live_dist": str(dist_dir),
                "previous_dist": str(previous_dist),
                "failed_live_dist": (
                    str(failed_live) if failed_live.exists() else ""
                ),
            },
        )
        if rollback_errors:
            raise PromotionRecoveryRequired(
                "In-place distribution update failed and rollback is incomplete: "
                + "; ".join(rollback_errors),
                recovery_root=package_root,
                rollback_errors=rollback_errors,
            ) from install_error
        raise

    recovery_manifest = _persist_package_document(
        package_root / "promotion-recovery-manifest.json",
        {
            "format_version": 1,
            "status": "retained",
            "strategy": "verified-in-place",
            "retained_at_utc": datetime.now(timezone.utc).isoformat(),
            "root_rename_error": str(root_rename_error),
            "live_dist": str(dist_dir),
            "recovery_root": str(package_root),
            "previous_dist": str(previous_dist),
            "retired_live_paths": str(retired_root),
            "staged_dist": str(staged_dist),
            "previous_tree_digest": retained_inventory["tree_digest"],
            "previous_entries": retained_inventory["entries"],
            "previous_package_digest": str(
                previous_metadata.get("payload_digest") or ""
            ),
            "unknown_live_app_paths": unknown_live_app_paths,
            "unknown_live_runtime_paths": unknown_live_runtime_paths,
            "runtime_paths_preserved": list(unknown_live_runtime_paths),
        },
    )
    _persist_package_document(
        package_root / "promotion-complete.json",
        {
            "format_version": 1,
            "status": "complete-with-recovery",
            "strategy": "verified-in-place",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "live_tree_digest": installed_inventory["tree_digest"],
            "previous_tree_digest": retained_inventory["tree_digest"],
            "recovery_manifest_digest": recovery_manifest["document_digest"],
        },
    )
    return package_root


def _restore_live_runtime_paths(
    previous_root: Path,
    live_root: Path,
    relative_paths: Sequence[str],
) -> list[str]:
    """Copy live runtime files carried by the previous distribution back into
    the promoted tree.  An update must not overwrite runtime state
    (settings/data/weights/sessions/unfinished tasks)."""
    restored: list[str] = []
    for relative_path in relative_paths:
        source = previous_root / Path(*relative_path.split("/"))
        if not source.is_file() or _is_link_or_reparse(source):
            continue
        target = live_root / Path(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(relative_path)
    return restored


def promote_staged_distribution(
    staged_dist: Path,
    dist_dir: Path,
) -> Path | None:
    staged_dist = _validate_staged_distribution(staged_dist)
    dist_dir = Path(os.path.abspath(dist_dir))
    package_root = staged_dist.parent
    staged_inventory = _inventory_package_tree(staged_dist)
    previous_dist = package_root / "previous-dist"
    failed_dist = package_root / "failed-new-dist"
    journal_path = package_root / "promotion-journal.json"

    live_info = _collect_live_distribution_info(dist_dir, staged_inventory)
    had_live_dist = live_info["had_live_dist"]
    previous_inventory = live_info["previous_inventory"]
    previous_metadata = live_info["previous_metadata"]
    unknown_live_app_paths = live_info["unknown_live_app_paths"]
    unknown_live_runtime_paths = live_info["unknown_live_runtime_paths"]

    _persist_promotion_journal(
        journal_path, package_root, staged_dist, dist_dir,
        previous_dist, staged_inventory, previous_inventory,
        unknown_live_app_paths, unknown_live_runtime_paths,
    )

    moved_previous = False
    installed_new = False
    try:
        dist_dir.parent.mkdir(parents=True, exist_ok=True)
        if had_live_dist:
            if previous_dist.exists() or previous_dist.is_symlink():
                raise RuntimeError(
                    f"Previous distribution recovery already exists: {previous_dist}"
                )
            try:
                os.replace(dist_dir, previous_dist)
            except PermissionError as root_rename_error:
                if previous_inventory is None:
                    raise
                return _promote_staged_distribution_in_place(
                    staged_dist=staged_dist,
                    dist_dir=dist_dir,
                    package_root=package_root,
                    previous_dist=previous_dist,
                    previous_inventory=previous_inventory,
                    previous_metadata=previous_metadata,
                    unknown_live_app_paths=unknown_live_app_paths,
                    unknown_live_runtime_paths=unknown_live_runtime_paths,
                    root_rename_error=root_rename_error,
                )
            moved_previous = True
        os.replace(staged_dist, dist_dir)
        installed_new = True

        installed_inventory = _inventory_package_tree(dist_dir)
        if installed_inventory != staged_inventory:
            raise RuntimeError(
                "Installed distribution does not match the staged package"
            )
        runtime_paths_restored: list[str] = []
        if moved_previous and unknown_live_runtime_paths:
            runtime_paths_restored = _restore_live_runtime_paths(
                previous_dist, dist_dir, unknown_live_runtime_paths
            )
        if moved_previous and previous_inventory is not None:
            retained_inventory = _inventory_package_tree(previous_dist)
            if retained_inventory != previous_inventory:
                raise RuntimeError(
                    "Previous distribution recovery verification failed"
                )
            recovery_manifest = _persist_package_document(
                package_root / "promotion-recovery-manifest.json",
                {
                    "format_version": 1,
                    "status": "retained",
                    "retained_at_utc": datetime.now(timezone.utc).isoformat(),
                    "live_dist": str(dist_dir),
                    "recovery_root": str(package_root),
                    "previous_dist": str(previous_dist),
                    "previous_tree_digest": retained_inventory["tree_digest"],
                    "previous_entries": retained_inventory["entries"],
                    "previous_package_digest": str(
                        previous_metadata.get("payload_digest") or ""
                    ),
                    "unknown_live_app_paths": unknown_live_app_paths,
                    "unknown_live_runtime_paths": unknown_live_runtime_paths,
                    "runtime_paths_restored": runtime_paths_restored,
                },
            )
            _persist_package_document(
                package_root / "promotion-complete.json",
                {
                    "format_version": 1,
                    "status": "complete-with-recovery",
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "live_tree_digest": installed_inventory["tree_digest"],
                    "previous_tree_digest": retained_inventory["tree_digest"],
                    "recovery_manifest_digest": recovery_manifest[
                        "document_digest"
                    ],
                },
            )
            return package_root

        _persist_package_document(
            package_root / "promotion-complete.json",
            {
                "format_version": 1,
                "status": "complete-new-install",
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "live_tree_digest": installed_inventory["tree_digest"],
            },
        )
        return None
    except BaseException as error:
        _handle_promotion_rollback(
            error, installed_new, moved_previous,
            dist_dir, failed_dist, previous_dist, package_root,
        )
