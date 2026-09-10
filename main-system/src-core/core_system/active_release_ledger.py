"""Activation ledger operations — A181/E156 append-only history.

Per A181 (certified-hot-update-persistence-and-reset-prevention), the
activation ledger is an append-only record of every activation and
rollback with from/to/reason/decision-proof/certificate/time.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from core_system.active_release_types import ActivationLedgerEntry

_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ACTIVATION_LEDGER_PATH: Final[Path] = (
    _DEFAULT_PROJECT_ROOT / "runtime" / "state"
    / "activation-ledger.jsonl"
)

# Public alias
ACTIVATION_LEDGER_PATH: Final[Path] = _ACTIVATION_LEDGER_PATH


def _ensure_state_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Append a JSON-Lines entry atomically to the activation ledger."""
    _ensure_state_dir(path)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _next_ledger_sequence(ledger_path: Path = _ACTIVATION_LEDGER_PATH) -> int:
    """Return the next sequence number for the activation ledger."""
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


def record_activation(
    *,
    operation: str,
    from_release_id: str,
    to_release_id: str,
    reason: str,
    decision_proof: str,
    certificate_digest: str,
    ledger_path: Path = _ACTIVATION_LEDGER_PATH,
) -> ActivationLedgerEntry:
    """Append an entry to the activation ledger (A181: HISTORY)."""
    entry = ActivationLedgerEntry(
        sequence=_next_ledger_sequence(ledger_path),
        operation=operation,
        from_release_id=from_release_id,
        to_release_id=to_release_id,
        reason=reason,
        decision_proof=decision_proof,
        certificate_digest=certificate_digest,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    _atomic_append_jsonl(ledger_path, entry.as_dict())
    return entry


def read_activation_ledger(
    *,
    ledger_path: Path = _ACTIVATION_LEDGER_PATH,
    limit: int | None = None,
) -> list[ActivationLedgerEntry]:
    """Read the activation ledger (newest first when limit is set)."""
    if not ledger_path.is_file():
        return []
    entries: list[ActivationLedgerEntry] = []
    try:
        with ledger_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                entries.append(ActivationLedgerEntry(
                    sequence=data["sequence"],
                    operation=data["operation"],
                    from_release_id=data["from_release_id"],
                    to_release_id=data["to_release_id"],
                    reason=data["reason"],
                    decision_proof=data["decision_proof"],
                    certificate_digest=data["certificate_digest"],
                    recorded_at=data["recorded_at"],
                ))
    except (OSError, json.JSONDecodeError, KeyError):
        return []
    if limit is not None:
        entries = entries[-limit:]
    return entries


__all__ = [
    "ACTIVATION_LEDGER_PATH",
    "read_activation_ledger",
    "record_activation",
]
