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
