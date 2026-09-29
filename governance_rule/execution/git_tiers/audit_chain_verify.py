"""Audit hash-chain verification surface (A185 split).

Extracted from ``audit_chain.py`` (source-size contract): segment/record
iteration, tail-window reads, full-segment + single-sequence + tail +
epoch-link verification and the supervisor-facing ``chain_health``
summary.  Read-only — never mutates chain state.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from .audit_chain_core import (
    CURRENT_FILE,
    GENESIS_HASH,
    _chain_dir,
    _load_manifest,
    _load_state,
    _record_hash,
)


def _iter_records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)
    except OSError:
        return


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_segment(
    path: Path, *, first_sequence: int, first_previous: str | None
) -> dict[str, Any]:
    """Verify one chained segment; returns a verification report.

    ``first_previous`` is the expected ``previous_hash`` of the first record
    (genesis for epoch 1, the prior segment's last record_hash afterwards).
    ``None`` accepts whatever the first record declares — used only where no
    boundary expectation exists.
    """
    expected_seq = first_sequence
    prev = first_previous
    count = 0
    last_hash = first_previous or GENESIS_HASH
    for record in _iter_records(path):
        count += 1
        claimed_hash = record.get("record_hash", "")
        body = dict(record)
        body.pop("record_hash", None)
        if int(record.get("sequence", -1)) != expected_seq:
            return {
                "valid": False,
                "reason": f"sequence gap at {expected_seq}",
                "records_checked": count,
            }
        if prev is not None and record.get("previous_hash") != prev:
            return {
                "valid": False,
                "reason": f"previous_hash mismatch at sequence {expected_seq}",
                "records_checked": count,
            }
        if claimed_hash != _record_hash(body):
            return {
                "valid": False,
                "reason": f"record_hash mismatch at sequence {expected_seq}",
                "records_checked": count,
            }
        prev = claimed_hash
        last_hash = claimed_hash
        expected_seq += 1
    return {
        "valid": True,
        "records_checked": count,
        "last_hash": last_hash,
        "next_sequence": expected_seq,
        "first_previous": records_first_previous(path),
    }


def records_first_previous(path: Path) -> str | None:
    """The first record's declared previous_hash (epoch boundary link)."""
    for record in _iter_records(path):
        return record.get("previous_hash")
    return None


def verify_current_chain() -> dict[str, Any]:
    """Recompute and verify the whole live chain segment."""
    directory = _chain_dir()
    state = _load_state(directory)
    manifest = _load_manifest(directory)
    epochs = manifest.get("epochs", [])
    current_epoch = next(
        (e for e in epochs if e.get("epoch") == state.get("epoch")), None
    )
    first_seq = int(
        (current_epoch or {}).get("first_sequence", state["next_sequence"])
    )
    # Record-chain continuity: the first record of this segment must carry
    # the last record_hash of the previous segment (or genesis at epoch 1).
    first_previous: str | None = GENESIS_HASH
    if int(state.get("epoch", 1)) > 1 and epochs:
        for epoch in reversed(epochs[:-1]):
            if epoch.get("last_record_hash"):
                first_previous = epoch["last_record_hash"]
                break
        else:
            first_previous = None
    report = _verify_segment(
        directory / CURRENT_FILE,
        first_sequence=first_seq,
        first_previous=first_previous,
    )
    report["state_consistent"] = report["valid"] and (
        report["next_sequence"] == state["next_sequence"]
        and report["last_hash"] == state["last_hash"]
    )
    report["epoch"] = state.get("epoch")
    return report


def verify_sequence(sequence: int) -> dict[str, Any]:
    """Verify a single record's hash and chain linkage."""
    directory = _chain_dir()
    for record in _iter_records(directory / CURRENT_FILE):
        if int(record.get("sequence", -1)) == sequence:
            body = dict(record)
            claimed = body.pop("record_hash", "")
            return {
                "valid": claimed == _record_hash(body),
                "sequence": sequence,
            }
    return {"valid": False, "sequence": sequence, "reason": "not-in-current-segment"}


def _tail_records(path: Path, count: int) -> list[dict[str, Any]]:
    """Read the last ``count`` records without scanning the whole file."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    # Records average well under 4 KiB; read a bounded tail window and
    # grow it if we still don't have enough complete lines.
    window = max(64 << 10, count * 4096)
    with path.open("rb") as handle:
        handle.seek(max(0, size - window))
        data = handle.read()
    lines = data.split(b"\n")
    # Drop a possibly-truncated first line unless we read from offset 0.
    if size > window:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for raw in lines[-count:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            return []  # torn tail read — report unverifiable, not corrupt
    return records


def verify_tail(count: int = 64) -> dict[str, Any]:
    """Verify the last ``count`` records chain to the recorded last_hash."""
    directory = _chain_dir()
    state = _load_state(directory)
    tail = _tail_records(directory / CURRENT_FILE, max(1, int(count)))
    if not tail:
        return {"valid": True, "records_checked": 0}
    prev = tail[0].get("previous_hash")
    checked = 0
    last_hash = prev
    for record in tail:
        body = dict(record)
        claimed = body.pop("record_hash", "")
        if record.get("previous_hash") != prev or claimed != _record_hash(body):
            return {"valid": False, "records_checked": checked}
        prev = claimed
        last_hash = claimed
        checked += 1
    return {
        "valid": last_hash == state.get("last_hash"),
        "records_checked": checked,
    }


def verify_epoch_link() -> dict[str, Any]:
    """Verify every archived epoch's final digest matches the next epoch's link."""
    directory = _chain_dir()
    manifest = _load_manifest(directory)
    epochs = manifest.get("epochs", [])
    problems: list[str] = []
    for index in range(1, len(epochs)):
        previous, current = epochs[index - 1], epochs[index]
        if current.get("previous_epoch_digest") != previous.get("final_digest"):
            problems.append(
                f"epoch {current.get('epoch')} link mismatch"
            )
        archive = previous.get("archive_path")
        if archive:
            archived = directory / archive
            if archived.is_file() and _file_digest(archived) != previous.get(
                "final_digest"
            ):
                problems.append(
                    f"epoch {previous.get('epoch')} archive digest mismatch"
                )
            # Record-hash continuity across the boundary: the first record
            # of the *current* segment must carry the previous segment's
            # last record_hash.
        if index == len(epochs) - 1 and previous.get("last_record_hash"):
            first_prev = records_first_previous(directory / CURRENT_FILE)
            if (
                first_prev is not None
                and first_prev != previous["last_record_hash"]
            ):
                problems.append(
                    f"epoch {current.get('epoch')} record-hash boundary mismatch"
                )
    return {"valid": not problems, "problems": problems}


def chain_health() -> dict[str, Any]:
    """Health summary for supervisor/report surfaces."""
    directory = _chain_dir()
    state = _load_state(directory)
    current = directory / CURRENT_FILE
    try:
        size = current.stat().st_size
    except OSError:
        size = 0
    manifest = _load_manifest(directory)
    last_rotation = ""
    for epoch in reversed(manifest.get("epochs", [])):
        if epoch.get("final_digest"):
            last_rotation = str(epoch.get("rotated_at") or epoch.get("created_at") or "")
            break
    verification = verify_tail()
    return {
        "epoch": state.get("epoch"),
        "audit_size": size,
        "audit_record_count": state.get("record_count", 0),
        "last_rotation": last_rotation,
        "chain_valid": bool(verification.get("valid")),
        "last_hash": state.get("last_hash", "")[:16],
    }


__all__ = [
    "chain_health", "records_first_previous", "verify_current_chain",
    "verify_epoch_link", "verify_sequence", "verify_tail",
    "_file_digest", "_iter_records", "_tail_records", "_verify_segment",
]
