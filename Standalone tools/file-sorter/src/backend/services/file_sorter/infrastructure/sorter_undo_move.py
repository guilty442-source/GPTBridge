"""Durable, no-overwrite file operations and per-target state for File Sorter.

The module intentionally has no dependency on ``main.py``.  This keeps the
transaction and profile repository usable by a future background service.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence




from .sorter_locks import (
    _fsync_directory,
)
from .sorter_metadata import (
    _capture_file_metadata,
    _file_metadata_from_dict,
    _fsync_file,
    _unlink_file_preserving_failure,
    _verify_file_metadata,
)
from .sorter_paths import (
    _is_link_or_reparse,
    _same_path_identity,
    _validated_target_directory,
)
from .sorter_stability import (
    same_volume,
)
from .sorter_staging import (
    _copy_file_exclusive_preserving_metadata,
    _publish_staging,
)
from .sorter_types import (
    SorterV2Error,
    _FileMetadata,
    sha256_file,
)


def _validate_undo_move_paths(
    moved_path: Path,
    original_path: Path,
) -> tuple[Path, Path]:
    target = _validated_target_directory(
        original_path.parent,
        label="Undo target",
    )
    moved_parent = moved_path.parent
    if (
        moved_parent.parent != target
        or not moved_parent.is_dir()
        or _is_link_or_reparse(moved_parent)
        or not _same_path_identity(
            moved_parent,
            moved_parent.resolve(strict=True),
        )
        or os.path.ismount(moved_parent)
        or moved_parent.stat().st_dev != target.stat().st_dev
    ):
        raise SorterV2Error(
            f"Undo moved path escaped the target folder: {moved_path}"
        )
    for candidate, label in (
        (moved_path, "Undo moved file"),
        (original_path, "Undo original file"),
    ):
        if candidate.exists() or candidate.is_symlink():
            if _is_link_or_reparse(candidate):
                raise SorterV2Error(f"{label} cannot be a link: {candidate}")
            resolved = candidate.resolve(strict=True)
            if (
                not _same_path_identity(candidate, resolved)
                or not resolved.is_file()
            ):
                raise SorterV2Error(f"{label} escaped its folder: {candidate}")
    return moved_path, original_path


def _undo_via_hardlink(
    source: Path,
    destination: Path,
    *,
    expected_hash: str,
    source_metadata: _FileMetadata,
    on_published: Callable[[str, _FileMetadata], None],
) -> bool:
    """Publish the undo through a hard link; False when unsupported."""

    try:
        os.link(source, destination)
    except FileExistsError as error:
        raise SorterV2Error(f"Undo source path is occupied: {destination}") from error
    except OSError as error:
        if error.errno not in {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            getattr(errno, "EOPNOTSUPP", 95),
            getattr(errno, "ENOTSUP", 95),
        }:
            raise
        return False
    _fsync_file(destination)
    _validate_undo_move_paths(source, destination)
    if not os.path.samefile(source, destination):
        raise SorterV2Error("Undo hard-link ownership proof failed.")
    _verify_file_metadata(
        destination,
        source_metadata,
        label="Undo hard-link publication",
    )
    on_published("hardlink", source_metadata)
    _validate_undo_move_paths(source, destination)
    if not os.path.samefile(source, destination):
        raise SorterV2Error(
            "Undo hard-link ownership changed after publication proof."
        )
    if sha256_file(source) != expected_hash:
        raise SorterV2Error(
            f"Moved file changed after undo publication: {source}"
        )
    _verify_file_metadata(source, source_metadata, label="Undo source")
    source.unlink()
    _fsync_directory(source.parent)
    _fsync_directory(destination.parent)
    return True


def _verify_undo_publish(
    destination: Path,
    expected_hash: str,
    source_metadata: _FileMetadata,
) -> None:
    if sha256_file(destination) != expected_hash:
        _unlink_file_preserving_failure(destination, missing_ok=True)
        raise SorterV2Error(f"Undo publish verification failed: {destination}")
    try:
        _verify_file_metadata(
            destination,
            source_metadata,
            label="Undo published file",
        )
    except SorterV2Error:
        _unlink_file_preserving_failure(destination, missing_ok=True)
        raise


def _verify_undo_restored_pair(
    source: Path,
    destination: Path,
    expected_hash: str,
    source_metadata: _FileMetadata,
) -> None:
    if sha256_file(source) != expected_hash:
        raise SorterV2Error(
            f"Moved file changed after undo publication: {source}"
        )
    if sha256_file(destination) != expected_hash:
        raise SorterV2Error(
            f"Restored file changed after undo publication: {destination}"
        )
    _verify_file_metadata(source, source_metadata, label="Undo source")
    _verify_file_metadata(
        destination,
        source_metadata,
        label="Undo published file",
    )


def _undo_via_staged_copy(
    source: Path,
    destination: Path,
    *,
    expected_hash: str,
    source_metadata: _FileMetadata,
    on_published: Callable[[str, _FileMetadata], None],
) -> None:
    stage = destination.parent / f".filesorter-undo-{uuid.uuid4().hex}.partial"
    stage_created = False
    try:
        _copy_file_exclusive_preserving_metadata(source, stage)
        stage_created = True
        if sha256_file(stage) != expected_hash:
            raise SorterV2Error(f"Undo staging verification failed: {source}")
        _verify_file_metadata(stage, source_metadata, label="Undo staging")
        _validate_undo_move_paths(source, destination)
        _publish_staging(stage, destination)
        _verify_undo_publish(destination, expected_hash, source_metadata)
        _verify_file_metadata(source, source_metadata, label="Undo source")
        _unlink_file_preserving_failure(stage, missing_ok=True)
        _fsync_directory(destination.parent)
        on_published("staged-copy", source_metadata)
        _validate_undo_move_paths(source, destination)
        _verify_undo_restored_pair(
            source,
            destination,
            expected_hash,
            source_metadata,
        )
        _unlink_file_preserving_failure(source)
        _fsync_directory(source.parent)
        _fsync_directory(destination.parent)
    finally:
        if stage_created:
            _unlink_file_preserving_failure(stage, missing_ok=True)


def _move_exact_for_undo(
    source: Path,
    destination: Path,
    *,
    expected_hash: str,
    on_published: Callable[[str, _FileMetadata], None],
) -> None:
    _validate_undo_move_paths(source, destination)
    if destination.exists() or destination.is_symlink():
        raise SorterV2Error(f"Undo source path is occupied: {destination}")
    if source.is_symlink() or not source.is_file():
        raise SorterV2Error(f"Moved file is missing: {source}")
    if sha256_file(source) != expected_hash:
        raise SorterV2Error(f"Moved file changed since transaction: {source}")
    source_metadata = _capture_file_metadata(source)
    if same_volume(source, destination.parent):
        if _undo_via_hardlink(
            source,
            destination,
            expected_hash=expected_hash,
            source_metadata=source_metadata,
            on_published=on_published,
        ):
            return
    _undo_via_staged_copy(
        source,
        destination,
        expected_hash=expected_hash,
        source_metadata=source_metadata,
        on_published=on_published,
    )


def _verify_undo_path_state(
    moved_path: Path,
    original_path: Path,
    expected_hash: str,
    moved_exists: bool,
    original_exists: bool,
) -> None:
    if moved_exists:
        if not moved_path.is_file() or sha256_file(moved_path) != expected_hash:
            raise SorterV2Error(
                f"Moved file changed since transaction: {moved_path}"
            )
    if original_exists:
        if not original_path.is_file() or sha256_file(original_path) != expected_hash:
            raise SorterV2Error(
                f"Restored file does not match the transaction: {original_path}"
            )


def _resolve_undo_both_exist(
    moved_path: Path,
    original_path: Path,
    expected_hash: str,
    operation: Mapping[str, Any],
) -> None:
    _validate_undo_move_paths(moved_path, original_path)
    publication_method = str(
        operation.get("undo_publication_method") or ""
    )
    publication_metadata = _file_metadata_from_dict(
        operation.get("undo_publication_metadata")
    )
    proof_matches = (
        operation.get("undo_publication_confirmed") is True
        and operation.get("undo_publication_sha256") == expected_hash
        and operation.get("undo_publication_source") == str(moved_path)
        and operation.get("undo_publication_destination") == str(original_path)
        and publication_method in {"hardlink", "staged-copy"}
        and publication_metadata is not None
    )
    if not proof_matches or publication_metadata is None:
        raise SorterV2Error(
            "Both undo paths exist without a durable publication proof; "
            "both copies were preserved for manual review."
        )
    _verify_file_metadata(
        moved_path,
        publication_metadata,
        label="Undo retained source",
    )
    _verify_file_metadata(
        original_path,
        publication_metadata,
        label="Undo published destination",
    )
    if publication_method == "hardlink" and not os.path.samefile(
        moved_path,
        original_path,
    ):
        raise SorterV2Error(
            "Undo hard-link publication proof no longer identifies one file; "
            "both copies were preserved."
        )
    _unlink_file_preserving_failure(moved_path)
    _fsync_directory(moved_path.parent)
    _fsync_directory(original_path.parent)


def _resume_undo_move(
    moved_path: Path,
    original_path: Path,
    *,
    expected_hash: str,
    operation: Mapping[str, Any],
    on_published: Callable[[str, _FileMetadata], None],
) -> None:
    _validate_undo_move_paths(moved_path, original_path)

    moved_exists = moved_path.exists()
    original_exists = original_path.exists()
    _verify_undo_path_state(
        moved_path,
        original_path,
        expected_hash,
        moved_exists,
        original_exists,
    )

    if moved_exists and not original_exists:
        _move_exact_for_undo(
            moved_path,
            original_path,
            expected_hash=expected_hash,
            on_published=on_published,
        )
        return
    if original_exists and not moved_exists:
        return
    if moved_exists and original_exists:
        _resolve_undo_both_exist(
            moved_path,
            original_path,
            expected_hash,
            operation,
        )
        return
    raise SorterV2Error(
        f"Neither the moved nor restored file exists: {moved_path}"
    )
