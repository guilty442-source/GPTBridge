"""Persistent task ledger for 星澄 native capability (A337/A46/A121).

法典依據:
- A337: 星澄 native-model programming + whole-system automation executor.
- A46: ledger-per-action — every task transition is an audit entry.
- A121: boundary enforcement + audit ledger + deny-on-violation.

Program and automation tasks are persisted in an append-only JSONL ledger
so they survive restarts.  Each task state transition (create, dispatch,
converge, verify, contain) appends a new entry; the prior entry is never
mutated.  The current state is reconstructed by replaying the ledger.

Authorization reference verification:
- ``user-command`` mode: the reference must be a registered command_code
  in the ``command_code_directory`` (A224).
- ``codex-mandate`` mode: the reference must be a valid dual-key grant
  minted by the permission sovereign (A435 two-key boundary).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DEFAULT_ROOT = Path(__file__).resolve().parents[4]
_TASK_LEDGER: Path = _DEFAULT_ROOT / "runtime" / "state" / "native-capability-tasks.jsonl"
_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append(entry: dict[str, Any]) -> None:
    """Append one entry to the task ledger (A46)."""
    try:
        _TASK_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        with _LOCK, _TASK_LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(line + os.linesep)
            handle.flush()
    except OSError:
        pass


def record_task_event(
    *,
    task_id: str,
    event: str,
    task_type: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record one task lifecycle event (create/dispatch/converge/verify/contain)."""
    _append({
        "timestamp": _iso_now(),
        "task_id": str(task_id),
        "event": str(event),
        "task_type": str(task_type),
        "detail": dict(detail) if detail else {},
    })


def load_tasks() -> dict[str, dict[str, Any]]:
    """Reconstruct current task state by replaying the ledger."""
    if not _TASK_LEDGER.is_file():
        return {}
    tasks: dict[str, dict[str, Any]] = {}
    try:
        with _TASK_LEDGER.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = entry.get("task_id", "")
                if not tid:
                    continue
                if tid not in tasks:
                    tasks[tid] = {"task_id": tid, "type": entry.get("task_type", "")}
                event = entry.get("event", "")
                detail = entry.get("detail", {})
                if event == "create":
                    tasks[tid].update(detail)
                    tasks[tid]["state"] = detail.get("state", "created")
                elif event == "dispatch":
                    tasks[tid]["state"] = "dispatched"
                    tasks[tid].update(detail)
                elif event == "converge":
                    tasks[tid]["state"] = "converged" if detail.get("converged") else tasks[tid].get("state", "")
                    tasks[tid].update(detail)
                elif event == "verify":
                    tasks[tid]["state"] = "verified"
                    tasks[tid].update(detail)
                elif event == "contain":
                    tasks[tid]["state"] = "contained"
                    tasks[tid].update(detail)
    except OSError:
        pass
    return tasks


def verify_authorization_reference(mode: str, reference: str) -> tuple[bool, str]:
    """Verify an automation authorization reference (A224/A435).

    Returns (valid, reason).  ``user-command`` mode requires the reference
    to be a registered command_code in the command_code_directory.
    ``codex-mandate`` mode requires a valid dual-key grant.
    """
    ref = str(reference or "").strip()
    if not ref:
        return False, "EMPTY_REFERENCE"
    if mode == "user-command":
        return _verify_user_command(ref)
    if mode == "codex-mandate":
        return _verify_codex_mandate(ref)
    return False, "UNKNOWN_MODE"


def _verify_user_command(reference: str) -> tuple[bool, str]:
    """Verify the reference is a registered command_code (A224)."""
    try:
        from governance_rule.execution.codex_reconcile import bounded_lookup
        codes = bounded_lookup(
            "xingcheng-fault-diagnostics",
            purpose="contract-gate",
            scope=("directory:command_code_directory",),
            reader=lambda ctx: {
                str(row.get("command_code", ""))
                for row in ctx.directory("command_code_directory")
            },
        )
        codes.discard("")
        if reference in codes:
            return True, "registered-command-code"
        return False, "UNREGISTERED_COMMAND_CODE"
    except Exception as error:
        return False, f"VERIFY_ERROR:{type(error).__name__}"


def _verify_codex_mandate(reference: str) -> tuple[bool, str]:
    """Verify the reference is a valid dual-key grant (A435).

    A codex mandate is a dual-key grant minted by the permission sovereign
    countersigning 星澄's automation request.  We verify the grant exists
    in the entry state and is not consumed or expired.
    """
    try:
        from governance_rule.execution.codex_entry_state import read_entry_state
        state = read_entry_state()
        grants = state.get("grants", {})
        record = grants.get(reference)
        if record is None:
            return False, "GRANT_NOT_FOUND"
        if record.get("consumed"):
            return False, "GRANT_CONSUMED"
        import time
        expires_at = record.get("expires_at", 0)
        if expires_at and time.time() > expires_at:
            return False, "GRANT_EXPIRED"
        if record.get("purpose") != "coordination":
            return False, "GRANT_PURPOSE_MISMATCH"
        return True, "valid-codex-mandate"
    except Exception as error:
        return False, f"VERIFY_ERROR:{type(error).__name__}"


__all__ = [
    "load_tasks",
    "record_task_event",
    "verify_authorization_reference",
]
