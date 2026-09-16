"""Atomic file writes and tombstone deletes.

Files must never be observable half-written: write to a temporary file,
fsync, hash, then atomically rename; only after the rename is verified does
PostgreSQL record the locator/hash.  Deletes are tombstones first — readers
stop, derived data is cleaned, the physical file goes last.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class AtomicFileError(RuntimeError):
    """Raised when a file write cannot be completed atomically."""


@dataclass(frozen=True)
class FileCommit:
    path: str
    size: int
    sha256: str


def write_atomic(path: str | Path, data: bytes, *, chunk_size: int = 1 << 20) -> FileCommit:
    """temp -> fsync -> hash -> os.replace (never a partial observable file)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    handle, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            for offset in range(0, len(data), chunk_size):
                chunk = data[offset : offset + chunk_size]
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise AtomicFileError(f"ATOMIC_WRITE_FAILED:{target}:{error}") from error
    return FileCommit(path=str(target), size=len(data), sha256=digest.hexdigest())


def verify_file(path: str | Path, expected_sha256: str) -> bool:
    target = Path(path)
    if not target.is_file():
        return False
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_sha256


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_committed(path: str | Path) -> bool:
    """Compensation for a committed file write (rename already happened)."""
    target = Path(path)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as error:
        raise AtomicFileError(f"FILE_REMOVE_FAILED:{target}:{error}") from error


def stage_tombstone(path: str | Path, *, suffix: str = ".tombstone") -> Path:
    """Mark a file unusable for readers before the physical delete."""
    target = Path(path)
    marker = target.with_name(target.name + suffix)
    marker.write_text("TOMBSTONED\n", encoding="ascii")
    return marker


__all__ = [
    "AtomicFileError",
    "FileCommit",
    "file_sha256",
    "remove_committed",
    "stage_tombstone",
    "verify_file",
    "write_atomic",
]
