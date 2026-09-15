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




from .sorter_journal import (
    _Journal,
)
from .sorter_locks import (
    _fsync_directory,
)
from .sorter_metadata import (
    _capture_file_metadata,
    _copy_windows_exclusive,
    _ensure_source_unchanged,
    _file_metadata_to_dict,
    _fsync_file,
    _unlink_file_preserving_failure,
    _verify_file_metadata,
)
from .sorter_paths import (
    _is_link_or_reparse,
    _validate_operation_paths,
)
from .sorter_types import (
    PlanOperation,
    SorterV2Error,
    sha256_file,
)


def _copy_file_exclusive_preserving_metadata(
    source: Path,
    destination: Path,
) -> None:
    if os.name == "nt":
        _copy_windows_exclusive(source, destination)
        _fsync_file(destination)
        return

    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        # copystat also copies extended attributes on platforms where Python
        # and the underlying filesystem support them.
        shutil.copystat(source, destination, follow_symlinks=False)
        _fsync_file(destination)
    except BaseException:
        # A failed exclusive copy may still contain diagnostic bytes. Keep the
        # transaction-owned partial instead of risking deletion after a path
        # replacement race; recovery will quarantine or review it.
        raise


def _copy_to_staging(
    source: Path,
    destination_dir: Path,
    transaction_id: str,
    operation_id: str,
) -> tuple[Path, str, int]:
    stage = destination_dir / (
        f".filesorter-{transaction_id[:8]}-{operation_id[:8]}.partial"
    )
    _copy_file_exclusive_preserving_metadata(source, stage)
    _fsync_directory(destination_dir)
    return stage, sha256_file(source), source.stat().st_size


def _copy_stage_exclusive(stage: Path, destination: Path) -> None:
    _copy_file_exclusive_preserving_metadata(stage, destination)


def _publish_staging(stage: Path, destination: Path) -> None:
    if os.name == "nt":
        # A Windows hard link shares the read-only attribute with the staging
        # name, which makes safe staging cleanup mutate the published file.
        # CopyFileW keeps them independent and still provides fail-if-exists.
        try:
            _copy_stage_exclusive(stage, destination)
        except FileExistsError as conflict:
            raise SorterV2Error(
                f"Destination appeared after preview: {destination}"
            ) from conflict
        _fsync_directory(destination.parent)
        return

    try:
        os.link(stage, destination)
        _fsync_file(destination)
    except FileExistsError:
        raise SorterV2Error(f"Destination appeared after preview: {destination}")
    except OSError as error:
        unsupported = {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            getattr(errno, "EOPNOTSUPP", 95),
            getattr(errno, "ENOTSUP", 95),
        }
        if error.errno not in unsupported:
            raise
        try:
            _copy_stage_exclusive(stage, destination)
        except FileExistsError as conflict:
            raise SorterV2Error(
                f"Destination appeared after preview: {destination}"
            ) from conflict
    _fsync_directory(destination.parent)


def _staged_move(
    source: Path,
    destination: Path,
    operation: PlanOperation,
    journal: _Journal,
    index: int,
) -> str:
    _validate_operation_paths(source.parent, operation)
    source_metadata = _capture_file_metadata(source)
    stage_name = (
        destination.parent
        / f".filesorter-{journal.transaction_id[:8]}-{operation.operation_id[:8]}.partial"
    )
    journal.update_operation(index, status="copying", staging=str(stage_name))
    stage, source_hash, bytes_copied = _copy_to_staging(
        source,
        destination.parent,
        journal.transaction_id,
        operation.operation_id,
    )
    _ensure_source_unchanged(source, operation)
    stage_stat = stage.stat()
    stage_hash = sha256_file(stage)
    if (
        bytes_copied != operation.source_size
        or stage_stat.st_size != operation.source_size
        or source_hash != stage_hash
    ):
        raise SorterV2Error(f"Staging verification failed for {source}")
    _verify_file_metadata(stage, source_metadata, label="Staging")
    journal.update_operation(
        index,
        status="verified",
        staging=str(stage),
        sha256=source_hash,
        metadata=_file_metadata_to_dict(source_metadata),
    )
    _validate_operation_paths(source.parent, operation)
    if _is_link_or_reparse(stage) or not stage.is_file():
        raise SorterV2Error(f"Staging path is no longer a regular file: {stage}")
    journal.update_operation(
        index,
        status="publishing",
        publication_confirmed=False,
        publication_method="staged-copy",
    )
    _publish_staging(stage, destination)
    if destination.stat().st_size != operation.source_size:
        raise SorterV2Error(
            f"Published size verification failed; preserved for review: {destination}"
        )
    if sha256_file(destination) != source_hash:
        raise SorterV2Error(
            "Published SHA-256 verification failed; preserved for review: "
            f"{destination}"
        )
    _verify_file_metadata(
        destination,
        source_metadata,
        label="Published file",
    )
    _unlink_file_preserving_failure(stage, missing_ok=True)
    _fsync_directory(destination.parent)
    journal.update_operation(
        index,
        status="published",
        staging=None,
        publication_confirmed=True,
        publication_method="staged-copy",
    )
    _validate_operation_paths(source.parent, operation)
    _ensure_source_unchanged(source, operation)
    if sha256_file(source) != source_hash:
        raise SorterV2Error(f"Source changed before deletion: {source}")
    _verify_file_metadata(source, source_metadata, label="Source")
    if sha256_file(destination) != source_hash:
        raise SorterV2Error(
            f"Published file changed before source deletion: {destination}"
        )
    _verify_file_metadata(
        destination,
        source_metadata,
        label="Published file before source deletion",
    )
    _unlink_file_preserving_failure(source)
    _fsync_directory(source.parent)
    journal.update_operation(index, status="committed")
    return source_hash


def _same_volume_move(
    source: Path,
    destination: Path,
    operation: PlanOperation,
    journal: _Journal,
    index: int,
) -> str:
    _validate_operation_paths(source.parent, operation)
    source_hash = sha256_file(source)
    _ensure_source_unchanged(source, operation)
    journal.update_operation(
        index,
        status="publishing",
        sha256=source_hash,
        publication_confirmed=False,
        publication_method="hardlink",
    )
    try:
        os.link(source, destination)
    except FileExistsError as error:
        raise SorterV2Error(
            f"Destination appeared after preview: {destination}"
        ) from error
    except OSError as error:
        unsupported = {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            getattr(errno, "EOPNOTSUPP", 95),
            getattr(errno, "ENOTSUP", 95),
        }
        if error.errno in unsupported:
            return _staged_move(source, destination, operation, journal, index)
        raise
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    if not os.path.samefile(source, destination):
        raise SorterV2Error(
            f"Published hard link does not identify the source file: {destination}"
        )
    journal.update_operation(
        index,
        status="published",
        publication_confirmed=True,
        publication_method="hardlink",
    )
    _validate_operation_paths(source.parent, operation)
    _ensure_source_unchanged(source, operation)
    if sha256_file(destination) != source_hash:
        raise SorterV2Error(f"Linked file changed before deletion: {destination}")
    if not os.path.samefile(source, destination):
        raise SorterV2Error(
            f"Published hard link changed before source deletion: {destination}"
        )
    source.unlink()
    _fsync_directory(source.parent)
    journal.update_operation(index, status="committed")
    return source_hash
