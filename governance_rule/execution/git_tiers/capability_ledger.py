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
import re
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

# §10.63 R2: append-only byte-offset index.  The ledger is audit history
# (tens of MB of JSONL) and every capability verification used to re-parse
# it; a full in-memory copy of the records costs well over a hundred MB of
# resident RSS.  Instead each line's byte offset is indexed by
# capability_id and nonce (~a few MB), so lookup/consumed/nonce_owner
# seek and decode only the matching lines.  The index is keyed by
# (st_ino, st_mtime_ns, st_size); appends extend it incrementally from
# the recorded size and a partial tail line is never indexed.
_INDEX: dict[
    str, tuple[int, int, int, dict[str, list[int]], dict[str, str]]
] = {}
_INDEX_LOCK = threading.Lock()

_CAP_RE = re.compile(r'"capability_id"\s*:\s*"((?:[^"\\]|\\.)*)"')
_NONCE_RE = re.compile(r'"nonce"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _ledger_lock(path: Path) -> threading.Lock:
    with _LEDGER_LOCKS_GUARD:
        return _LEDGER_LOCKS.setdefault(str(path), threading.Lock())


def _unescape(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw


def _index_keys(line: str) -> tuple[list[str], str | None]:
    """(capability_ids, nonce) for indexing without a full parse.

    Every ``capability_id`` occurrence is indexed — a foreign record whose
    top-level key is not serialized first must not silently drop out of
    ``lookup`` (that would let a mutated token evade detection).  Readers
    re-verify the parsed record, so over-indexing only wastes a seek.
    """
    caps: list[str] = []
    for match in _CAP_RE.finditer(line):
        cap = _unescape(match.group(1))
        if cap not in caps:
            caps.append(cap)
    nonce_match = _NONCE_RE.search(line)
    nonce = _unescape(nonce_match.group(1)) if nonce_match else None
    if (not caps and '"capability_id"' in line) or (
        nonce is None and '"nonce"' in line
    ):
        # Foreign layout the regex missed — fall back to a real parse so
        # a valid entry is never silently dropped from the index.
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if isinstance(record, dict):
            if not caps and isinstance(record.get("capability_id"), str):
                caps.append(record["capability_id"])
            if nonce is None and isinstance(record.get("nonce"), str):
                nonce = record["nonce"]
    return caps, nonce


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
        # Extend the offset index in place when this append lands exactly at
        # the recorded tail.  If another process interleaved a line, the
        # stamp is left alone so the next read re-indexes the whole gap.
        key = str(self.path)
        encoded = (line + "\n").encode("utf-8")
        try:
            stat = self.path.stat()
        except OSError:
            return entry
        with _INDEX_LOCK:
            cached = _INDEX.get(key)
            if (
                cached is not None
                and cached[0] == stat.st_ino
                and stat.st_size - cached[2] == len(encoded)
            ):
                cap = entry.get("capability_id")
                nonce = entry.get("nonce")
                if isinstance(cap, str) and cap:
                    cached[3].setdefault(cap, []).append(cached[2])
                if isinstance(nonce, str) and nonce:
                    cached[4].setdefault(nonce, str(cap or ""))
                _INDEX[key] = (
                    stat.st_ino, stat.st_mtime_ns, stat.st_size,
                    cached[3], cached[4],
                )
        return entry

    def _index(
        self,
    ) -> tuple[dict[str, list[int]], dict[str, str]] | None:
        """(capability_id -> [offsets], nonce -> capability_id) index."""
        key = str(self.path)
        try:
            stat = self.path.stat()
        except OSError:
            return None
        ino, mtime, size = stat.st_ino, stat.st_mtime_ns, stat.st_size
        with _INDEX_LOCK:
            cached = _INDEX.get(key)
            if cached is not None and cached[:3] == (ino, mtime, size):
                return cached[3], cached[4]
        caps: dict[str, list[int]] = {}
        nonces: dict[str, str] = {}
        offset = 0
        if cached is not None and cached[0] == ino and size > cached[2]:
            # Same file, appended only — index just the new tail.
            caps = {cap: list(offs) for cap, offs in cached[3].items()}
            nonces = dict(cached[4])
            offset = cached[2]
        scanned = offset
        try:
            with self.path.open("rb") as handle:
                handle.seek(offset)
                while True:
                    pos = handle.tell()
                    raw = handle.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        break  # partial tail — index it on the next scan
                    scanned = handle.tell()
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        line = stripped.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    caps_found, nonce = _index_keys(line)
                    for cap in caps_found:
                        caps.setdefault(cap, []).append(pos)
                    if nonce:
                        nonces.setdefault(nonce, str(caps_found[0] if caps_found else ""))
        except OSError:
            return None
        with _INDEX_LOCK:
            _INDEX[key] = (ino, mtime, scanned, caps, nonces)
        return caps, nonces

    def entries(self) -> list[dict[str, Any]]:
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
        return records

    def lookup(self, capability_id: str) -> list[dict[str, Any]]:
        indexed = self._index()
        if indexed is None:
            return []
        offsets = indexed[0].get(capability_id)
        if not offsets:
            return []
        records: list[dict[str, Any]] = []
        try:
            with self.path.open("rb") as handle:
                for offset in list(offsets):
                    handle.seek(offset)
                    try:
                        record = json.loads(handle.readline().decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if (
                        isinstance(record, dict)
                        and record.get("capability_id") == capability_id
                    ):
                        records.append(record)
        except OSError:
            return []
        return records

    def consumed(self, capability_id: str) -> bool:
        return any(
            e.get("result") == "consumed" and e.get("consumed_at")
            for e in self.lookup(capability_id)
        )

    def nonce_owner(self, nonce: str) -> str | None:
        indexed = self._index()
        if indexed is None:
            return None
        return indexed[1].get(nonce)

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
