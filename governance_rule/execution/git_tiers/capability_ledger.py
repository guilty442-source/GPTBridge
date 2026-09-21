"""Append-only capability ledger (JSONL) for single-use verification.

Every capability interaction is recorded as one JSON line — issuance,
consumption, denial and execution completion.  Records carry the required
``capability_id / nonce / issued / consumed_at / command_id / result`` set
plus the binding digest used to detect payload mutation (extended expiry,
changed scope, revived id).

The ledger is append-only: no method rewrites or truncates history.  A
thread lock serializes appends inside the process; the file is opened in
append mode so concurrent processes cannot interleave a line.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .capability_time import utc_now_iso

if TYPE_CHECKING:  # pragma: no cover - annotation only (no runtime cycle)
    from .capability import CapabilityToken

__all__ = [
    "CAPABILITY_LEDGER_ENV",
    "DEFAULT_LEDGER_PATH",
    "CapabilityLedger",
]

CAPABILITY_LEDGER_ENV = "GPTBRIDGE_CAPABILITY_LEDGER"
DEFAULT_LEDGER_PATH = (
    Path(__file__).resolve().parents[1] / "audit" / "capability_ledger.jsonl"
)

_LEDGER_LOCKS: dict[str, threading.Lock] = {}
_LEDGER_LOCKS_GUARD = threading.Lock()

# §10.63 R2: mtime+size-keyed parse cache.  The ledger is append-only, so a
# stat stamp uniquely identifies its content — repeated verification calls
# inside one governed command reused to re-parse tens of MB per call.
_ENTRIES_CACHE: dict[str, tuple[int, int, list[dict[str, Any]]]] = {}
_ENTRIES_CACHE_LOCK = threading.Lock()


def _ledger_lock(path: Path) -> threading.Lock:
    with _LEDGER_LOCKS_GUARD:
        return _LEDGER_LOCKS.setdefault(str(path), threading.Lock())


def _record(token: "CapabilityToken", *, result: str) -> dict[str, Any]:
    from .capability import token_payload_digest

    return {
        "capability_id": token.capability_id,
        "nonce": token.nonce,
        "issued": token.issued_at,
        "consumed_at": "",
        "command_id": "",
        "result": result,
        "token_digest": token_payload_digest(token),
        "actor": token.actor,
        "operation": token.operation,
        "tier": token.tier,
        "repository_id": token.repository_id,
        "policy_version": token.policy_version,
        "policy_digest": token.policy_digest,
        "integrity": token.integrity_state(),
        "approval_path": "CAPABILITY_TOKEN",
    }


class CapabilityLedger:
    """Append-only JSONL ledger: issued / consumed / denied evidence."""

    def __init__(self, path: str | Path | None = None) -> None:
        override = os.environ.get(CAPABILITY_LEDGER_ENV, "").strip()
        self.path = Path(path or override or DEFAULT_LEDGER_PATH)

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        entry = dict(record)
        entry.setdefault("recorded_at", utc_now_iso())
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        with _ledger_lock(self.path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        # Keep the parse cache warm for readers in this process; other
        # processes' appends are picked up via the stat stamp.
        key = str(self.path)
        try:
            stat = self.path.stat()
        except OSError:
            return entry
        with _ENTRIES_CACHE_LOCK:
            cached = _ENTRIES_CACHE.get(key)
            if cached is not None:
                cached[2].append(dict(entry))
                _ENTRIES_CACHE[key] = (stat.st_mtime_ns, stat.st_size, cached[2])
        return entry

    def entries(self) -> list[dict[str, Any]]:
        key = str(self.path)
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return []
        with _ENTRIES_CACHE_LOCK:
            cached = _ENTRIES_CACHE.get(key)
            if cached is not None and cached[0] == stamp[0] and cached[1] == stamp[1]:
                return [dict(e) for e in cached[2]]
        records: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        with _ENTRIES_CACHE_LOCK:
            _ENTRIES_CACHE[key] = (stamp[0], stamp[1], records)
        return [dict(e) for e in records]

    def lookup(self, capability_id: str) -> list[dict[str, Any]]:
        return [
            e for e in self.entries()
            if e.get("capability_id") == capability_id
        ]

    def consumed(self, capability_id: str) -> bool:
        return any(
            e.get("result") == "consumed" and e.get("consumed_at")
            for e in self.lookup(capability_id)
        )

    def nonce_owner(self, nonce: str) -> str | None:
        for entry in self.entries():
            if entry.get("nonce") == nonce:
                return str(entry.get("capability_id") or "")
        return None

    def record_issue(self, token: "CapabilityToken") -> dict[str, Any]:
        return self.append(_record(token, result="issued"))

    def record_consume(
        self, token: "CapabilityToken", *, command_id: str = "",
        result: str = "consumed", approval_path: str = "CAPABILITY_TOKEN",
    ) -> dict[str, Any]:
        record = _record(token, result=result)
        record["consumed_at"] = utc_now_iso()
        record["command_id"] = command_id or uuid.uuid4().hex
        record["approval_path"] = approval_path
        return self.append(record)

    def record_denial(
        self, token: "CapabilityToken", code: str, detail: str = "",
    ) -> dict[str, Any]:
        record = _record(token, result=f"denied:{code}")
        record["detail"] = detail
        return self.append(record)

    def record_result(
        self, token: "CapabilityToken", *, command_id: str = "",
        result: str = "executed", approval_path: str = "CAPABILITY_TOKEN",
    ) -> dict[str, Any]:
        record = _record(token, result=result)
        record["command_id"] = command_id
        record["approval_path"] = approval_path
        record["completed_at"] = utc_now_iso()
        return self.append(record)

    def record_legacy(
        self, *, actor: str, operation: str, command_id: str, detail: str,
        approval_path: str, repository_id: str = "",
    ) -> dict[str, Any]:
        return self.append(
            {
                "actor": actor,
                "operation": operation,
                "command_id": command_id,
                "result": "legacy-authorized",
                "approval_path": approval_path,
                "repository_id": repository_id,
                "consumed_at": utc_now_iso(),
                "detail": detail,
            }
        )
