"""G54 unified task lifecycle contract tests."""
from __future__ import annotations

import json
from pathlib import Path

import _main_system_test_support as _support  # noqa: F401
import pytest

from governance.sovereigns.xingcheng import native_capability_task_ledger as ledger


def _identity() -> ledger.TaskIdentity:
    return ledger.TaskIdentity(
        task_id="task-1",
        request_id="request-1",
        operation_id="operation-1",
        execution_receipt="receipt-1",
        idempotency_key="idem-1",
    )


def test_g54_happy_path_persists_all_states(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "tasks.jsonl"
    monkeypatch.setattr(ledger, "_TASK_LEDGER", path)
    identity = _identity()
    states = [
        ledger.TaskState.CREATED,
        ledger.TaskState.VALIDATED,
        ledger.TaskState.AUTHORIZED,
        ledger.TaskState.QUEUED,
        ledger.TaskState.RUNNING,
        ledger.TaskState.COMPLETED,
    ]

    for current, target in zip(states, states[1:]):
        ledger.record_task_transition(
            identity=identity,
            current=current,
            target=target,
            task_type="program",
        )

    tasks = ledger.load_tasks()
    assert tasks["task-1"]["state"] == "COMPLETED"
    assert tasks["task-1"]["request_id"] == "request-1"
    assert tasks["task-1"]["operation_id"] == "operation-1"
    assert tasks["task-1"]["execution_receipt"] == "receipt-1"
    assert tasks["task-1"]["idempotency_key"] == "idem-1"


def test_g54_rejects_invalid_transition() -> None:
    try:
        ledger.validate_task_transition(ledger.TaskState.CREATED, ledger.TaskState.RUNNING)
    except ValueError as error:
        assert str(error) == "TASK_INVALID_TRANSITION:CREATED->RUNNING"
    else:
        raise AssertionError("invalid transition was accepted")


def test_g54_recovery_requires_verification_before_resume(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "tasks.jsonl"
    monkeypatch.setattr(ledger, "_TASK_LEDGER", path)
    identity = _identity()

    ledger.record_task_transition(
        identity=identity,
        current=ledger.TaskState.RUNNING,
        target=ledger.TaskState.INTERRUPTED,
        task_type="program",
    )
    ledger.record_task_transition(
        identity=identity,
        current=ledger.TaskState.INTERRUPTED,
        target=ledger.TaskState.RECOVERING,
        task_type="program",
    )
    with pytest.raises(ValueError, match="TASK_RECOVERY_VERIFICATION_REQUIRED"):
        ledger.record_task_transition(
            identity=identity,
            current=ledger.TaskState.RECOVERING,
            target=ledger.TaskState.QUEUED,
            task_type="program",
        )

    ledger.record_task_transition(
        identity=identity,
        current=ledger.TaskState.RECOVERING,
        target=ledger.TaskState.QUEUED,
        task_type="program",
        verification={"verified": True, "evidence": "receipt-1"},
    )

    entries = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[-1]["detail"]["recovery_verification"]["verified"] is True
