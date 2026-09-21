"""Codex amendment request lifecycle and single-lineage lock (G71).

The official Codex remains read-only.  This module manages only request-side
metadata: request identity, predecessor lineage, lifecycle state, candidate
hash, audit result and closure evidence.  Its lock is a filesystem lineage
lock under an explicit ledger root; it serializes candidate construction for
one predecessor generation so two same-generation requests cannot fork the
same authority lineage.

Fail-closed rules:

* a request must carry ``artifact=codex-amendment-request``, a request id,
  ``authority=request-only`` and a predecessor version/history head;
* ``not_executed`` must be true while a worker-side request is active;
* a request id may not be reused with different content;
* only one active request may hold a predecessor lineage;
* stale predecessor identity is denied before construction;
* state transitions are explicit and terminal states release the lineage;
* every transition is persisted atomically before the lock is released.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from governance_rule.execution.codex_amendment import (
    CodexAmendmentDenied,
    normalize_change_class,
)
from governance_rule.execution.codex_amendment_contract import content_hash

REQUEST_ARTIFACT: Final[str] = "codex-amendment-request"
REQUEST_AUTHORITY: Final[str] = "request-only"
LIFECYCLE_SCHEMA: Final[str] = "gptbridge-codex-amendment-lifecycle/v1"
DEFAULT_LEDGER_ROOT: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "main-system"
    / "runtime"
    / "state"
    / "codex-amendments"
)

STATE_SUBMITTED: Final[str] = "submitted"
STATE_UNDER_REVIEW: Final[str] = "under-review"
STATE_SUCCESSOR_BUILT: Final[str] = "successor-built"
STATE_AUDITING: Final[str] = "auditing"
STATE_AUDIT_PASSED: Final[str] = "audit-passed"
STATE_READY_FOR_GOVERNOR: Final[str] = "ready-for-governor"
STATE_EXECUTED: Final[str] = "executed"
STATE_REJECTED: Final[str] = "rejected"
STATE_WITHDRAWN: Final[str] = "withdrawn"

TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {STATE_EXECUTED, STATE_REJECTED, STATE_WITHDRAWN}
)

STATE_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    STATE_SUBMITTED: frozenset(
        {STATE_UNDER_REVIEW, STATE_REJECTED, STATE_WITHDRAWN}
    ),
    STATE_UNDER_REVIEW: frozenset(
        {STATE_SUCCESSOR_BUILT, STATE_REJECTED, STATE_WITHDRAWN}
    ),
    STATE_SUCCESSOR_BUILT: frozenset(
        {STATE_AUDITING, STATE_REJECTED, STATE_WITHDRAWN}
    ),
    STATE_AUDITING: frozenset({STATE_AUDIT_PASSED, STATE_REJECTED}),
    STATE_AUDIT_PASSED: frozenset(
        {STATE_READY_FOR_GOVERNOR, STATE_REJECTED, STATE_WITHDRAWN}
    ),
    STATE_READY_FOR_GOVERNOR: frozenset(
        {STATE_EXECUTED, STATE_REJECTED, STATE_WITHDRAWN}
    ),
}


class AmendmentLifecycleError(RuntimeError):
    """Fail-closed lifecycle/lineage denial."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AmendmentRequest:
    request_id: str
    request_hash: str
    predecessor: Mapping[str, Any]
    lineage_key: str
    payload: Mapping[str, Any]
    scope: tuple[str, ...]


@dataclass(frozen=True)
class LifecycleRecord:
    request_id: str
    state: str
    request_hash: str
    lineage_key: str
    record_path: Path
    lock_path: Path
    not_executed: bool = True
    history: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": LIFECYCLE_SCHEMA,
            "request_id": self.request_id,
            "state": self.state,
            "request_hash": self.request_hash,
            "lineage_key": self.lineage_key,
            "record_path": str(self.record_path),
            "lock_path": str(self.lock_path),
            "not_executed": self.not_executed,
            "history": [dict(item) for item in self.history],
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip()).strip("-.")
    if not safe:
        raise AmendmentLifecycleError("REQUEST_ID_REQUIRED")
    return safe[:160]


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AmendmentLifecycleError("REQUEST_UNREADABLE", str(error)) from error
    if not isinstance(payload, Mapping):
        raise AmendmentLifecycleError("REQUEST_NOT_AN_OBJECT")
    return payload


def request_scope(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract the deterministic mutation/rebind scope for lineage evidence."""
    scope: set[str] = set()
    for item in payload.get("changes") or ():
        if isinstance(item, Mapping):
            table = str(item.get("table") or "").strip()
            field = str(item.get("field") or "").strip()
            if table:
                scope.add(f"table:{table}")
            if table and field:
                scope.add(f"field:{table}.{field}")
    successors = payload.get("proposed_successors") or ()
    if isinstance(successors, Mapping):
        successors = (successors,)
    for item in successors:
        if not isinstance(item, Mapping):
            continue
        registry = str(item.get("registry") or "").strip()
        if registry:
            scope.add(f"registry:{registry}")
        provision = item.get("provision")
        if isinstance(provision, Mapping):
            artifact = str(provision.get("architecture_artifact") or "").strip()
            if artifact:
                scope.add(f"artifact:{artifact}")
        artifact = str(item.get("architecture_artifact") or "").strip()
        if artifact:
            scope.add(f"artifact:{artifact}")
    singular = payload.get("proposed_successor")
    if isinstance(singular, Mapping):
        scope.add("provision:" + str(singular.get("provision_id") or "pending"))
    proposed_change = payload.get("proposed_change")
    if isinstance(proposed_change, Mapping):
        table = str(proposed_change.get("table") or "").strip()
        if table:
            scope.add(f"table:{table}")
    for proposal_key in ("proposed_repair", "proposed_resolution"):
        if isinstance(payload.get(proposal_key), Mapping):
            scope.add(f"proposal:{proposal_key}")
    return tuple(sorted(scope))


def load_amendment_request(path: str | Path) -> AmendmentRequest:
    """Load and validate one request artifact without executing it."""
    request_path = Path(path)
    payload = _load_json(request_path)
    if str(payload.get("artifact") or "").strip() != REQUEST_ARTIFACT:
        raise AmendmentLifecycleError("REQUEST_ARTIFACT_INVALID")
    if str(payload.get("authority") or "").strip() != REQUEST_AUTHORITY:
        raise AmendmentLifecycleError("REQUEST_AUTHORITY_INVALID")
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise AmendmentLifecycleError("REQUEST_ID_REQUIRED")
    if not str(payload.get("requested_by") or "").strip():
        raise AmendmentLifecycleError("REQUEST_REQUESTER_REQUIRED")
    try:
        normalize_change_class(str(payload.get("change_class") or ""))
    except CodexAmendmentDenied as error:
        raise AmendmentLifecycleError(
            "REQUEST_CHANGE_CLASS_INVALID", str(error)
        ) from error
    if not str(payload.get("required_review") or "").strip():
        raise AmendmentLifecycleError("REQUEST_REVIEW_REQUIRED")
    predecessor = payload.get("predecessor")
    if not isinstance(predecessor, Mapping):
        raise AmendmentLifecycleError("REQUEST_PREDECESSOR_REQUIRED")
    predecessor_version = str(predecessor.get("codex_version") or "").strip()
    history_head = str(predecessor.get("history_head") or "").strip()
    if not predecessor_version or not history_head:
        raise AmendmentLifecycleError("REQUEST_LINEAGE_REQUIRED")
    if payload.get("not_executed") is not True:
        raise AmendmentLifecycleError("REQUEST_ALREADY_CLOSED")
    if not any(
        payload.get(key)
        for key in (
            "changes",
            "proposed_successors",
            "proposed_successor",
            "proposed_change",
            "proposed_repair",
            "proposed_resolution",
        )
    ):
        raise AmendmentLifecycleError("REQUEST_SUCCESSOR_REQUIRED")
    request_hash = content_hash(payload)
    lineage_key = content_hash(
        {
            "codex_version": predecessor_version,
            "history_head": history_head,
            "revision_sequence": predecessor.get("revision_sequence"),
        }
    )
    return AmendmentRequest(
        request_id=request_id,
        request_hash=request_hash,
        predecessor=dict(predecessor),
        lineage_key=lineage_key,
        payload=payload,
        scope=request_scope(payload),
    )


class CodexAmendmentRequestLedger:
    """Append/request-state ledger plus one active lock per lineage."""

    def __init__(self, root: str | Path = DEFAULT_LEDGER_ROOT) -> None:
        self.root = Path(root).resolve()
        self.records_dir = self.root / "requests"
        self.locks_dir = self.root / "lineage-locks"

    def _record_path(self, request_id: str) -> Path:
        return self.records_dir / f"{_safe_name(request_id)}.json"

    def _lock_path(self, lineage_key: str) -> Path:
        return self.locks_dir / f"lineage-{lineage_key[:32]}.lock"

    def load_record(self, request_id: str) -> dict[str, Any] | None:
        path = self._record_path(request_id)
        if not path.is_file():
            return None
        payload = _load_json(path)
        return dict(payload)

    def _load_record_required(self, request_id: str) -> dict[str, Any]:
        record = self.load_record(request_id)
        if record is None:
            raise AmendmentLifecycleError("REQUEST_RECORD_REQUIRED", request_id)
        return record

    def _acquire_lineage(self, request: AmendmentRequest) -> Path:
        lock_path = self._lock_path(request.lineage_key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": LIFECYCLE_SCHEMA,
            "request_id": request.request_id,
            "request_hash": request.request_hash,
            "lineage_key": request.lineage_key,
            "predecessor": dict(request.predecessor),
            "scope": list(request.scope),
            "created_at": _utc_now(),
        }
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _load_json(lock_path)
            existing_request = str(existing.get("request_id") or "")
            if existing_request == request.request_id:
                return lock_path
            raise AmendmentLifecycleError(
                "REQUEST_LINEAGE_LOCKED",
                f"{request.lineage_key} held by {existing_request or 'unknown'}",
            )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        return lock_path

    def _release_lineage(self, record: Mapping[str, Any]) -> None:
        raw_lock_path = str(record.get("lock_path") or "")
        if not raw_lock_path:
            return
        lock_path = Path(raw_lock_path)
        try:
            lock = _load_json(lock_path)
        except AmendmentLifecycleError:
            return
        if str(lock.get("request_id")) == str(record.get("request_id")):
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _record_invalid_request(
        self, request_path: str | Path, error: AmendmentLifecycleError
    ) -> None:
        """Close malformed request artifacts instead of leaving them ambiguous."""
        path = Path(request_path)
        request_id = path.stem
        request_hash = ""
        try:
            payload = _load_json(path)
            request_id = str(payload.get("request_id") or request_id).strip()
            request_hash = content_hash(payload)
        except AmendmentLifecycleError:
            try:
                request_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                request_hash = ""
        try:
            request_id = _safe_name(request_id)
        except AmendmentLifecycleError:
            request_id = _safe_name(path.stem)
        existing = self.load_record(request_id)
        if existing is not None:
            existing_hash = str(existing.get("request_hash") or "")
            if existing_hash and request_hash and existing_hash != request_hash:
                return
            if str(existing.get("state") or "") in TERMINAL_STATES:
                return
        record_path = self._record_path(request_id)
        record = {
            "schema": LIFECYCLE_SCHEMA,
            "request_id": request_id,
            "state": STATE_REJECTED,
            "request_hash": request_hash,
            "lineage_key": "",
            "request_path": str(path.resolve()),
            "predecessor": {},
            "scope": [],
            "record_path": str(record_path),
            "lock_path": "",
            "not_executed": True,
            "closed_at": _utc_now(),
            "history": [
                {
                    "at": _utc_now(),
                    "from": "",
                    "to": STATE_REJECTED,
                    "evidence": {"error": str(error)},
                }
            ],
        }
        _atomic_json(record_path, record)

    def _validate_lineage(
        self,
        request: AmendmentRequest,
        *,
        current_version: str | None,
        expected_revision_sequence: int | None,
    ) -> None:
        if current_version is not None and (
            str(request.predecessor.get("codex_version")) != str(current_version)
        ):
            raise AmendmentLifecycleError(
                "STALE_PREDECESSOR_VERSION",
                f"{request.predecessor.get('codex_version')} != {current_version}",
            )
        sequence = request.predecessor.get("revision_sequence")
        if expected_revision_sequence is not None and sequence != expected_revision_sequence:
            raise AmendmentLifecycleError(
                "STALE_REVISION_SEQUENCE",
                f"{sequence} != {expected_revision_sequence}",
            )

    def begin(
        self,
        request_path: str | Path,
        *,
        current_version: str | None = None,
        expected_revision_sequence: int | None = None,
    ) -> LifecycleRecord:
        """Register a request and acquire its predecessor lineage lock."""
        try:
            request = load_amendment_request(request_path)
        except AmendmentLifecycleError as error:
            self._record_invalid_request(request_path, error)
            raise
        existing = self.load_record(request.request_id)
        if existing is not None:
            if str(existing.get("request_hash")) != request.request_hash:
                raise AmendmentLifecycleError(
                    "REQUEST_ID_REUSE", request.request_id
                )
            state = str(existing.get("state") or "")
            if state in TERMINAL_STATES:
                raise AmendmentLifecycleError(
                    "REQUEST_ALREADY_TERMINAL", f"{request.request_id}:{state}"
                )
            try:
                self._validate_lineage(
                    request,
                    current_version=current_version,
                    expected_revision_sequence=expected_revision_sequence,
                )
            except AmendmentLifecycleError as error:
                try:
                    self.reject(
                        request.request_id,
                        reason=str(error),
                        evidence={"stale_at": _utc_now()},
                    )
                except AmendmentLifecycleError:
                    pass
                raise
            lock_path = Path(str(existing.get("lock_path") or ""))
            if not lock_path.is_file():
                lock_path = self._acquire_lineage(request)
                existing["lock_path"] = str(lock_path)
                _atomic_json(Path(str(existing["record_path"])), existing)
            return self._record_from_payload(existing)
        self._validate_lineage(
            request,
            current_version=current_version,
            expected_revision_sequence=expected_revision_sequence,
        )
        lock_path = self._acquire_lineage(request)
        record_path = self._record_path(request.request_id)
        record = {
            "schema": LIFECYCLE_SCHEMA,
            "request_id": request.request_id,
            "state": STATE_SUBMITTED,
            "request_hash": request.request_hash,
            "lineage_key": request.lineage_key,
            "request_path": str(Path(request_path).resolve()),
            "predecessor": dict(request.predecessor),
            "scope": list(request.scope),
            "record_path": str(record_path),
            "lock_path": str(lock_path),
            "not_executed": True,
            "history": [
                {
                    "at": _utc_now(),
                    "from": "",
                    "to": STATE_SUBMITTED,
                    "evidence": {},
                }
            ],
        }
        _atomic_json(record_path, record)
        return self._record_from_payload(record)

    def transition(
        self,
        request_id: str,
        target: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> LifecycleRecord:
        """Move one request through the explicit lifecycle state machine."""
        record = self._load_record_required(request_id)
        current = str(record.get("state") or "")
        target_state = str(target or "").strip()
        if current in TERMINAL_STATES:
            raise AmendmentLifecycleError(
                "REQUEST_STATE_TERMINAL", f"{request_id}:{current}"
            )
        if target_state not in STATE_TRANSITIONS.get(current, frozenset()):
            raise AmendmentLifecycleError(
                "REQUEST_STATE_TRANSITION_DENIED",
                f"{request_id}:{current}->{target_state}",
            )
        history = list(record.get("history") or [])
        history.append(
            {
                "at": _utc_now(),
                "from": current,
                "to": target_state,
                "evidence": dict(evidence or {}),
            }
        )
        record["state"] = target_state
        record["history"] = history
        if target_state == STATE_EXECUTED:
            record["not_executed"] = False
            record["execution_record"] = dict(evidence or {})
        if target_state in TERMINAL_STATES:
            record["closed_at"] = _utc_now()
        _atomic_json(Path(str(record["record_path"])), record)
        if target_state in TERMINAL_STATES:
            self._release_lineage(record)
        return self._record_from_payload(record)

    def reject(
        self, request_id: str, *, reason: str, evidence: Mapping[str, Any] | None = None
    ) -> LifecycleRecord:
        payload = {"reason": reason, **dict(evidence or {})}
        return self.transition(request_id, STATE_REJECTED, evidence=payload)

    def _record_from_payload(self, payload: Mapping[str, Any]) -> LifecycleRecord:
        return LifecycleRecord(
            request_id=str(payload.get("request_id") or ""),
            state=str(payload.get("state") or ""),
            request_hash=str(payload.get("request_hash") or ""),
            lineage_key=str(payload.get("lineage_key") or ""),
            record_path=Path(str(payload.get("record_path") or "")),
            lock_path=Path(str(payload.get("lock_path") or "")),
            not_executed=bool(payload.get("not_executed")),
            history=tuple(payload.get("history") or ()),
        )


__all__ = [
    "AmendmentLifecycleError",
    "AmendmentRequest",
    "CodexAmendmentRequestLedger",
    "DEFAULT_LEDGER_ROOT",
    "LIFECYCLE_SCHEMA",
    "LifecycleRecord",
    "REQUEST_ARTIFACT",
    "REQUEST_AUTHORITY",
    "STATE_AUDIT_PASSED",
    "STATE_AUDITING",
    "STATE_EXECUTED",
    "STATE_READY_FOR_GOVERNOR",
    "STATE_REJECTED",
    "STATE_SUBMITTED",
    "STATE_SUCCESSOR_BUILT",
    "STATE_UNDER_REVIEW",
    "STATE_WITHDRAWN",
    "TERMINAL_STATES",
    "load_amendment_request",
    "request_scope",
]
