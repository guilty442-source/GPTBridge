"""Hash-chained audit ledger for Git governance records (A375 AUDIT).

Existing flat ``git_tier_audit.jsonl`` history is never rewritten — it is
``legacy-unhashed`` evidence.  Starting from a new chain epoch every appended
record carries:

  sequence        monotonically increasing within the chain
  previous_hash   record_hash of the preceding chained record
  record_hash     sha256 over the canonicalized payload (record minus the
                  hash field itself)

Layout under ``governance_rule/execution/audit/git_audit_chain/``::

    current.jsonl             live chain segment (one JSON record per line)
    chain_state.json          {epoch, next_sequence, last_hash, record_count}
    audit_chain_manifest.json epoch list with first_sequence, digests, dates
    archive/YYYY-MM/          rotated segments (immutable once finalized)

Append path: cross-process byte-range lock -> append -> fsync -> update
chain state (A375: threading.Lock alone is insufficient).

Module layout (A185 source-size split):

    audit_chain_core.py    layout constants, env knobs, lock, hashing, IO
    audit_chain_verify.py  read-only verification + chain_health surface
    audit_chain.py         append / epoch rotation / retention (this module)
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .audit_chain_core import (  # noqa: F401  (re-exported primitives)
    ARCHIVE_DIR,
    CHAIN_MANIFEST_FILE,
    CHAIN_STATE_FILE,
    CURRENT_FILE,
    GENESIS_HASH,
    LEGACY_MARKER,
    _canonical,
    _chain_dir,
    _chain_retention_hours,
    _chain_rotation_bytes,
    _load_manifest,
    _load_state,
    _locked,
    _now_utc,
    _read_json,
    _record_hash,
    _write_json_atomic,
)
from .audit_chain_verify import (  # noqa: F401  (re-exported verify surface)
    chain_health,
    records_first_previous,
    verify_current_chain,
    verify_epoch_link,
    verify_sequence,
    verify_tail,
    _file_digest,
    _iter_records,
    _tail_records,
    _verify_segment,
)


def _prune_archived_segments(directory: Path) -> None:
    """Retire rotated (finalized) chain segments older than the window."""
    retention = _chain_retention_hours()
    if retention <= 0:
        return
    cutoff = time.time() - retention * 3600
    archive_root = directory / ARCHIVE_DIR
    if not archive_root.is_dir():
        return
    for candidate in archive_root.rglob("*.jsonl"):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            continue


def _ensure_epoch(directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Ensure the manifest has an open epoch row matching ``state``."""
    manifest = _load_manifest(directory)
    if manifest["epochs"] and manifest["epochs"][-1].get("epoch") == state["epoch"]:
        return manifest
    manifest["epochs"].append(
        {
            "epoch": state["epoch"],
            "first_sequence": state["next_sequence"],
            "previous_epoch_digest": (
                manifest["epochs"][-1].get("final_digest")
                if manifest["epochs"]
                else GENESIS_HASH
            ),
            "created_at": _now_utc(),
            "file": CURRENT_FILE,
            "final_digest": None,
        }
    )
    _write_json_atomic(directory / CHAIN_MANIFEST_FILE, manifest)
    return manifest


def _maybe_auto_rotate(directory: Path) -> None:
    """Rotate the live segment when it exceeds the size ceiling.

    Called outside ``audit-append.lock`` (``rotate`` takes that lock
    itself).  Racing callers are safe: once one rotates, the live segment
    is tiny and subsequent calls return ``below-threshold``/``empty``.
    """
    ceiling = _chain_rotation_bytes()
    if ceiling <= 0:
        return
    current = directory / CURRENT_FILE
    try:
        if current.stat().st_size < ceiling:
            return
    except OSError:
        return
    try:
        rotate(max_bytes=ceiling)
    except Exception:
        return


def append_audit(record: dict[str, Any]) -> dict[str, Any]:
    """Append one record to the hash chain; returns the chained record."""
    directory = _chain_dir()
    _maybe_auto_rotate(directory)
    lock_path = directory / "audit-append.lock"
    with _locked(lock_path):
        state = _load_state(directory)
        _ensure_epoch(directory, state)
        sequence = int(state["next_sequence"])
        chained = dict(record)
        chained["sequence"] = sequence
        chained["epoch"] = state["epoch"]
        chained["previous_hash"] = state["last_hash"]
        chained["record_hash"] = _record_hash(chained)
        current = directory / CURRENT_FILE
        with current.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(chained) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        state["next_sequence"] = sequence + 1
        state["last_hash"] = chained["record_hash"]
        state["record_count"] = int(state.get("record_count", 0)) + 1
        _write_json_atomic(directory / CHAIN_STATE_FILE, state)
        return chained


def rotate(*, max_bytes: int | None = None, force: bool = False) -> dict[str, Any]:
    """Rotate the live segment into a monthly archive partition.

    flush -> lock -> final digest -> manifest -> rename -> new epoch start
    record.  Sequence continuity is preserved: the next epoch's first record
    keeps ``previous_hash`` pointing at the rotated segment's last
    ``record_hash`` so the chain is never cut.
    """
    directory = _chain_dir()
    lock_path = directory / "audit-append.lock"
    with _locked(lock_path):
        state = _load_state(directory)
        current = directory / CURRENT_FILE
        try:
            size = current.stat().st_size
        except OSError:
            size = 0
        if not force and max_bytes is not None and size < max_bytes:
            return {"rotated": False, "reason": "below-threshold", "size": size}
        if not current.is_file() or size == 0:
            return {"rotated": False, "reason": "empty", "size": size}
        final_digest = _file_digest(current)
        month = time.strftime("%Y-%m", time.gmtime())
        archive_dir = directory / ARCHIVE_DIR / month
        archive_dir.mkdir(parents=True, exist_ok=True)
        first_seq = 1
        manifest = _load_manifest(directory)
        epochs = manifest.get("epochs", [])
        if epochs:
            first_seq = int(epochs[-1].get("first_sequence", 1))
        last_seq = int(state["next_sequence"]) - 1
        archive_name = f"epoch-{state['epoch']}-seq-{first_seq}-{last_seq}.jsonl"
        archive_path = archive_dir / archive_name
        os.replace(current, archive_path)

        # Close out the manifest epoch and open the next one.
        manifest = _load_manifest(directory)
        if manifest["epochs"]:
            manifest["epochs"][-1]["final_digest"] = final_digest
            manifest["epochs"][-1]["archive_path"] = str(
                archive_path.relative_to(directory)
            )
            manifest["epochs"][-1]["rotated_at"] = _now_utc()
            manifest["epochs"][-1]["last_sequence"] = last_seq
            manifest["epochs"][-1]["last_record_hash"] = state["last_hash"]
        _write_json_atomic(directory / CHAIN_MANIFEST_FILE, manifest)

        state["epoch"] = int(state["epoch"]) + 1
        state["current_file"] = CURRENT_FILE
        _write_json_atomic(directory / CHAIN_STATE_FILE, state)
        _ensure_epoch(directory, state)

        # First record of the new segment: an epoch-start marker that
        # carries the rotated digest as its previous_hash so the hash chain
        # is continuous across the rotation boundary.
        chained = {
            "event": "epoch-start",
            "timestamp": _now_utc(),
            "epoch": state["epoch"],
            "rotated_digest": final_digest,
            "sequence": int(state["next_sequence"]),
            "previous_hash": state["last_hash"],
        }
        chained["record_hash"] = _record_hash(chained)
        with current.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(chained) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        state["next_sequence"] = int(state["next_sequence"]) + 1
        state["last_hash"] = chained["record_hash"]
        _write_json_atomic(directory / CHAIN_STATE_FILE, state)
        _ensure_epoch(directory, state)
        _prune_archived_segments(directory)
        return {
            "rotated": True,
            "epoch": state["epoch"],
            "archive": str(archive_path),
            "final_digest": final_digest,
        }


def chained_audit_log(
    tier: int,
    command: str,
    actor: str,
    approved: bool,
    detail: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """audit_log + hash-chain append in one call (A375 AUDIT)."""
    from governance_rule.execution import git_tiers

    entry = git_tiers.audit_log(tier, command, actor, approved, detail, **kwargs)
    try:
        append_audit(dict(entry))
    except Exception:
        pass
    return entry


def legacy_status(ledger_path: Path) -> dict[str, Any]:
    """Mark the pre-chain flat ledger honestly as ``legacy-unhashed``."""
    try:
        size = ledger_path.stat().st_size
    except OSError:
        size = 0
    return {"path": str(ledger_path), "size": size, "chain": LEGACY_MARKER}


__all__ = [
    "append_audit",
    "chain_health",
    "legacy_status",
    "rotate",
    "verify_current_chain",
    "verify_epoch_link",
    "verify_sequence",
    "verify_tail",
]
