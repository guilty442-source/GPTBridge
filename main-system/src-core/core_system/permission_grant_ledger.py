"""Permission grant ledger — append-only JSONL persistence (A46/A174).

The permission sovereign's issued grants and lifecycle transitions are
recorded in an append-only JSON-Lines ledger so they survive restarts,
carry version and revocation evidence, and can be audited without
in-memory state.

Each entry records: sequence, timestamp, operation (issue/terminate/
renew/restrict/suspend/revoke), permission_id, actor, capability, target,
status, requester, review finding, and basis references.  Entries are
never mutated; a lifecycle transition appends a new entry with the new
status rather than overwriting the prior record.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_PERMISSION_LEDGER_PATH: Final[Path] = (
    _DEFAULT_PROJECT_ROOT / "runtime" / "state" / "permission-grant-ledger.jsonl"
)

PERMISSION_LEDGER_PATH: Final[Path] = _PERMISSION_LEDGER_PATH

_LOCK = threading.Lock()


def _ensure_state_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_sequence(ledger_path: Path = _PERMISSION_LEDGER_PATH) -> int:
    """Return the next sequence number for the permission ledger."""
    if not ledger_path.is_file():
        return 1
    count = 0
    try:
        with ledger_path.open("r", encoding="utf-8") as handle:
            for _ in handle:
                count += 1
    except OSError:
        pass
    return count + 1


def _append_entry(entry: dict[str, Any], ledger_path: Path = _PERMISSION_LEDGER_PATH) -> int:
    """Append one entry to the ledger and return its sequence number."""
    _ensure_state_dir(ledger_path)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    with _LOCK, ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return int(entry["sequence"])


def record_grant(
    *,
    permission_id: str,
    actor: str,
    capability: str,
    target: str,
    action: str,
    data_scope: str,
    requester: str,
    review_finding: str = "",
    review_id: str = "",
    basis: tuple[str, ...] = (),
    ledger_path: Path = _PERMISSION_LEDGER_PATH,
) -> int:
    """Record a permission issuance (issue) in the ledger."""
    entry = {
        "sequence": _next_sequence(ledger_path),
        "timestamp": _iso_now(),
        "operation": "issue",
        "permission_id": str(permission_id),
        "actor": str(actor),
        "capability": str(capability),
        "target": str(target),
        "action": str(action),
        "data_scope": str(data_scope),
        "status": "issued",
        "requester": str(requester),
        "review_finding": str(review_finding),
        "review_id": str(review_id),
        "basis": list(basis),
    }
    return _append_entry(entry, ledger_path)


def record_lifecycle(
    *,
    operation: str,
    permission_id: str,
    requester: str,
    basis: tuple[str, ...] = (),
    detail: dict[str, Any] | None = None,
    ledger_path: Path = _PERMISSION_LEDGER_PATH,
) -> int:
    """Record a lifecycle transition (terminate/renew/restrict/suspend/revoke).

    The transition is appended as a new entry; the prior entry is never
    mutated.  ``detail`` may carry restrictions or other metadata.
    """
    if operation not in ("terminate", "renew", "restrict", "suspend", "revoke"):
        raise ValueError(f"invalid lifecycle operation: {operation!r}")
    entry = {
        "sequence": _next_sequence(ledger_path),
        "timestamp": _iso_now(),
        "operation": operation,
        "permission_id": str(permission_id),
        "status": operation + "d",  # terminated, renewed, ...
        "requester": str(requester),
        "basis": list(basis),
        "detail": dict(detail) if detail else {},
    }
    return _append_entry(entry, ledger_path)


def load_grant_history(
    permission_id: str, ledger_path: Path = _PERMISSION_LEDGER_PATH
) -> list[dict[str, Any]]:
    """Return the full append-only history for one permission_id."""
    if not ledger_path.is_file():
        return []
    history: list[dict[str, Any]] = []
    try:
        with ledger_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("permission_id") == permission_id:
                    history.append(entry)
    except OSError:
        pass
    return history


def current_status(
    permission_id: str, ledger_path: Path = _PERMISSION_LEDGER_PATH
) -> dict[str, Any] | None:
    """Return the most recent ledger entry for a permission_id, or None."""
    history = load_grant_history(permission_id, ledger_path)
    if not history:
        return None
    return history[-1]


def was_issued(
    permission_id: str, ledger_path: Path = _PERMISSION_LEDGER_PATH
) -> bool:
    """True if the permission_id has at least one 'issue' entry in the ledger."""
    for entry in load_grant_history(permission_id, ledger_path):
        if entry.get("operation") == "issue":
            return True
    return False


__all__ = [
    "PERMISSION_LEDGER_PATH",
    "current_status",
    "load_grant_history",
    "record_grant",
    "record_lifecycle",
    "was_issued",
]
