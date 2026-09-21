"""G54 unified task lifecycle contract tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import _main_system_test_support as _support  # noqa: F401
import pytest

from core_system.codex_decision import SovereignRequest
from governance.sovereigns.xingcheng import native_capability as native
from governance.sovereigns.xingcheng import native_capability_task_ledger as ledger


def _identity() -> ledger.TaskIdentity:
    return ledger.TaskIdentity(
        task_id="task-1",
        request_id="request-1",
        operation_id="operation-1",
        execution_receipt="receipt-1",
        idempotency_key="idem-1",
    )


class _NativeHarness(native.XingchengNativeMixin):
    def __init__(self) -> None:
        self._program_tasks = {}
        self._automation_tasks = {}
        self.app = None

    @staticmethod
    def _resolve_in_domain(path: str) -> Path:
        return Path(path)

    @staticmethod
    def _iso_now() -> str:
        return "2026-09-21T00:00:00Z"

    @staticmethod
    def verified_basis(*tokens: str) -> tuple[str, ...]:
        return tokens


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


def test_g54_program_entry_writes_identity_and_terminal_state(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "tasks.jsonl"
    monkeypatch.setattr(ledger, "_TASK_LEDGER", path)

    async def execute(_app, _task):
        return {"status": "executed", "result": "ok"}

    monkeypatch.setattr(native, "_invoke_native_model_executor", execute)
    outcome = asyncio.run(
        _NativeHarness()._adjudicate_program(
            SovereignRequest(
                intent="program.analyze",
                subject="source.py",
                requester="test",
                payload={
                    "task_id": "program-test",
                    "request_id": "request-test",
                    "operation_id": "operation-test",
                    "execution_receipt": "receipt-test",
                    "idempotency_key": "idem-test",
                },
            )
        )
    )

    assert outcome.accepted is True
    task = outcome.result["task"]
    assert task["lifecycle_state"] == "COMPLETED"
    assert task["operation_id"] == "operation-test"
    assert ledger.load_tasks()["program-test"]["state"] == "COMPLETED"


def test_g54_automation_decompose_and_schedule_preserve_identity(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "tasks.jsonl"
    monkeypatch.setattr(ledger, "_TASK_LEDGER", path)
    harness = _NativeHarness()
    request = SovereignRequest(
        intent="automation.decompose",
        subject="maintenance",
        requester="test",
        payload={
            "task_id": "auto-test",
            "request_id": "request-auto",
            "operation_id": "operation-auto",
            "execution_receipt": "receipt-auto",
            "idempotency_key": "idem-auto",
            "steps": [{"step": "inspect"}],
        },
    )

    created = harness._automation_decompose(request)
    assert created.accepted is True
    scheduled = harness._automation_schedule(
        SovereignRequest(
            intent="automation.schedule",
            subject="maintenance",
            requester="test",
            payload={"task_id": "auto-test"},
        )
    )

    assert scheduled.accepted is True
    assert scheduled.result["state"] == "QUEUED"
    task = ledger.load_tasks()["auto-test"]
    assert task["request_id"] == "request-auto"
    assert task["operation_id"] == "operation-auto"
    assert task["state"] == "QUEUED"
