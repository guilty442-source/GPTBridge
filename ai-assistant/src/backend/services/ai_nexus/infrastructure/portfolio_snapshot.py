"""Non-locking file I/O and portfolio snapshot management."""

from __future__ import annotations

import io
import os
import re
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .portfolio_constants import (
    DEFAULT_IMPORT_SNAPSHOT_KEEP,
    SNAPSHOT_COPY_ATTEMPTS,
    SNAPSHOT_COPY_CHUNK_BYTES,
)
from .portfolio_models import InvestmentManagerError


@contextmanager
def _open_portfolio_source_shared(source: Path) -> Iterator[BinaryIO]:
    if os.name != "nt":
        with source.open("rb") as source_file:
            yield source_file
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_sequential_scan = 0x08000000

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
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(source),
        generic_read,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_attribute_normal | file_flag_sequential_scan,
        None,
    )
    invalid_handle_value = wintypes.HANDLE(-1).value
    if handle in (None, invalid_handle_value):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, ctypes.FormatError(error_code), str(source))

    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | os.O_BINARY,
        )
    except OSError:
        close_handle(handle)
        raise

    with os.fdopen(descriptor, "rb") as source_file:
        yield source_file


def _source_revision_unchanged(before: os.stat_result, after: os.stat_result) -> bool:
    return before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns


def _read_portfolio_source_bytes(source: Path) -> bytes:
    for attempt in range(SNAPSHOT_COPY_ATTEMPTS):
        with _open_portfolio_source_shared(source) as source_file:
            revision_before = os.fstat(source_file.fileno())
            content = source_file.read()
            revision_after = os.fstat(source_file.fileno())
        if _source_revision_unchanged(revision_before, revision_after):
            return content
        if attempt + 1 < SNAPSHOT_COPY_ATTEMPTS:
            time.sleep(0.05)
    raise InvestmentManagerError(
        "Portfolio file kept changing while a non-locking read was in progress."
    )


def _read_portfolio_source_text(source: Path, *, encoding: str) -> str:
    return _read_portfolio_source_bytes(source).decode(encoding)


@contextmanager
def _open_xlsx_workbook_unlocked(path: Path) -> Iterator[zipfile.ZipFile]:
    workbook_bytes = _read_portfolio_source_bytes(path)
    with io.BytesIO(workbook_bytes) as workbook_stream:
        with zipfile.ZipFile(workbook_stream) as workbook:
            yield workbook


def _copy_portfolio_snapshot_once(source: Path, partial_target: Path) -> bool:
    with _open_portfolio_source_shared(source) as source_file:
        revision_before = os.fstat(source_file.fileno())
        with partial_target.open("xb") as target_file:
            while chunk := source_file.read(SNAPSHOT_COPY_CHUNK_BYTES):
                target_file.write(chunk)
        revision_after = os.fstat(source_file.fileno())
    return _source_revision_unchanged(revision_before, revision_after)


def _is_owned_snapshot_partial(partial_target: Path, target: Path) -> bool:
    """Prove a partial belongs to this snapshot transaction before cleanup."""

    if partial_target.parent != target.parent or partial_target.is_symlink():
        return False
    prefix = f".{target.name}."
    suffix = ".partial"
    name = partial_target.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return False
    token = name[len(prefix) : -len(suffix)]
    return bool(re.fullmatch(r"[a-f0-9]{32}", token))


def create_portfolio_file_snapshot(
    source: Path,
    imports_root: Path,
    *,
    keep: int = DEFAULT_IMPORT_SNAPSHOT_KEEP,
) -> Path:
    imports_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._-")
    safe_stem = (safe_stem or "portfolio")[:80]
    target = imports_root / f"{stamp}-{uuid.uuid4().hex}-{safe_stem}{source.suffix}"
    last_error: OSError | InvestmentManagerError | None = None
    for attempt in range(SNAPSHOT_COPY_ATTEMPTS):
        partial_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
        try:
            if not _copy_portfolio_snapshot_once(source, partial_target):
                last_error = InvestmentManagerError(
                    "Portfolio file changed while the import snapshot was being created."
                )
            else:
                if target.exists() or target.is_symlink():
                    raise InvestmentManagerError(
                        "Snapshot publication target already exists; refusing to overwrite it."
                    )
                partial_target.replace(target)
                prune_portfolio_file_snapshots(imports_root, keep=keep)
                return target
        except OSError as exc:
            last_error = exc
        finally:
            if _is_owned_snapshot_partial(partial_target, target):
                try:
                    partial_target.unlink(missing_ok=True)
                except OSError:
                    pass
        if attempt + 1 < SNAPSHOT_COPY_ATTEMPTS:
            time.sleep(0.05)

    raise InvestmentManagerError(
        f"Unable to create a non-locking portfolio import snapshot: {last_error}"
    ) from last_error


def prune_portfolio_file_snapshots(
    imports_root: Path,
    *,
    keep: int = DEFAULT_IMPORT_SNAPSHOT_KEEP,
) -> dict[str, Any]:
    """Report logical archive pressure without deleting source snapshots."""

    try:
        snapshots = sorted(
            (
                item
                for item in imports_root.iterdir()
                if item.is_file() and not item.name.endswith(".partial")
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return {
            "retained_count": 0,
            "logical_archive_count": 0,
            "storage_pressure": False,
            "automatic_delete": False,
        }
    active_limit = max(0, int(keep))
    logical_archive_count = max(0, len(snapshots) - active_limit)
    return {
        "retained_count": len(snapshots),
        "active_window_count": min(len(snapshots), active_limit),
        "logical_archive_count": logical_archive_count,
        "storage_pressure": logical_archive_count > max(100, active_limit * 5),
        "automatic_delete": False,
    }
