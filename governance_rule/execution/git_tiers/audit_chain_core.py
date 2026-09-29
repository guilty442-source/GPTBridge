"""Audit hash-chain primitives (A185 split).

Extracted from ``audit_chain.py`` (source-size contract): chain layout
constants, environment-tunable rotation/retention knobs, the
cross-process byte-range lock, canonicalization/record hashing, atomic
JSON IO and the state/manifest loaders shared by the append path
(``audit_chain``) and the verification surface (``audit_chain_verify``).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_CHAIN_DIR = (
    Path(__file__).resolve().parents[1] / "audit" / "git_audit_chain"
)
CURRENT_FILE = "current.jsonl"
CHAIN_STATE_FILE = "chain_state.json"
CHAIN_MANIFEST_FILE = "audit_chain_manifest.json"
ARCHIVE_DIR = "archive"
GENESIS_HASH = "0" * 64
LEGACY_MARKER = "legacy-unhashed"


_CHAIN_DIR_ENV = "GPTBRIDGE_AUDIT_CHAIN_DIR"

# Auto-rotate the live chain segment at a size ceiling so an unbounded
# append cannot exhaust disk while governed automation is busy.  Rotation
# preserves sequence/hash continuity (see ``rotate``); retention prunes
# only rotated, finalized segments older than the window.
_CHAIN_ROTATION_BYTES_ENV = "GPTBRIDGE_AUDIT_CHAIN_ROTATION_BYTES"
_CHAIN_ROTATION_BYTES_DEFAULT = 128 << 20
_CHAIN_RETENTION_HOURS_ENV = "GPTBRIDGE_AUDIT_CHAIN_RETENTION_HOURS"
_CHAIN_RETENTION_HOURS_DEFAULT = 24


def _chain_rotation_bytes() -> int:
    raw = os.environ.get(_CHAIN_ROTATION_BYTES_ENV, "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _CHAIN_ROTATION_BYTES_DEFAULT
    return value if value > 0 else _CHAIN_ROTATION_BYTES_DEFAULT


def _chain_retention_hours() -> int:
    raw = os.environ.get(_CHAIN_RETENTION_HOURS_ENV, "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _CHAIN_RETENTION_HOURS_DEFAULT
    return value if value > 0 else _CHAIN_RETENTION_HOURS_DEFAULT


def _chain_dir() -> Path:
    override = os.environ.get(_CHAIN_DIR_ENV, "").strip()
    return Path(override) if override else _CHAIN_DIR


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Cross-process byte-range lock (Windows msvcrt / POSIX fcntl)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def _record_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _load_state(directory: Path) -> dict[str, Any]:
    state = _read_json(directory / CHAIN_STATE_FILE, None)
    if not isinstance(state, dict) or "epoch" not in state:
        state = {
            "epoch": 1,
            "next_sequence": 1,
            "last_hash": GENESIS_HASH,
            "record_count": 0,
            "current_file": CURRENT_FILE,
            "created_at": _now_utc(),
        }
    return state


def _load_manifest(directory: Path) -> dict[str, Any]:
    manifest = _read_json(directory / CHAIN_MANIFEST_FILE, None)
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("epochs"), list
    ):
        manifest = {"schema": "audit-chain-manifest-v1", "epochs": []}
    return manifest


__all__ = [
    "ARCHIVE_DIR", "CHAIN_MANIFEST_FILE", "CHAIN_STATE_FILE", "CURRENT_FILE",
    "GENESIS_HASH", "LEGACY_MARKER",
    "_canonical", "_chain_dir", "_chain_retention_hours",
    "_chain_rotation_bytes", "_load_manifest", "_load_state", "_locked",
    "_now_utc", "_read_json", "_record_hash", "_write_json_atomic",
]
