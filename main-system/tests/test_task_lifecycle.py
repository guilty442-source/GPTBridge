"""§10.16 Task Lifecycle tests — unified state machine + idempotency."""

from __future__ import annotations

import json

from core_system.task_lifecycle import (
    AUTHORIZED,
    CANCELLED,
    COMPLETED,
    CREATED,
    FAILED,
    INTERRUPTED,
    QUEUED,
    RECOVERING,
    RUNNING,
    TASK_LIFECYCLE_VERSION,
    TIMED_OUT,
    TaskLifecycleRegistry,
    VALIDATED,
    transition_is_valid,
)


def _to_running(reg, op_id, *, generation="gen-1"):
    """沿合法路徑走 CREATED→VALIDATED→AUTHORIZED→QUEUED→RUNNING。"""
    assert reg.validate(op_id).ok
    assert reg.authorize(op_id).ok
    assert reg.queue(op_id).ok
    started = reg.start(op_id, backend_generation=generation)
    assert started.ok, started.reason
    return started


def test_transition_matrix_sane():
    assert transition_is_valid(CREATED, VALIDATED)
    assert transition_is_valid(VALIDATED, AUTHORIZED)
    assert transition_is_valid(AUTHORIZED, QUEUED)
    assert transition_is_valid(QUEUED, RUNNING)
    assert transition_is_valid(RUNNING, COMPLETED)
    assert transition_is_valid(RUNNING, INTERRUPTED)
    assert transition_is_valid(INTERRUPTED, RECOVERING)
    assert transition_is_valid(RECOVERING, RUNNING)
    # 終止狀態不得再轉出
    assert not transition_is_valid(COMPLETED, RUNNING)
    assert not transition_is_valid(FAILED, RUNNING)
    assert not transition_is_valid(CANCELLED, RUNNING)
    assert not transition_is_valid(TIMED_OUT, RECOVERING)


def test_full_happy_path(tmp_path):
    reg = TaskLifecycleRegistry(tmp_path / "tc.json")
    created = reg.create("file-sorter::categorize", idempotency_key="k1")
    assert created.ok and created.record["status"] == CREATED
    op = created.record["operation_id"]

    assert reg.validate(op).ok
    assert reg.authorize(op).ok
    assert reg.queue(op).ok
    started = reg.start(op, backend_generation="gen-41")
    assert started.ok
    assert started.record["backend_generation"] == "gen-41"
    assert started.record["attempt_count"] == 1
    assert reg.complete(op).ok

    final = reg.get(op)
    assert final.status == COMPLETED
    assert final.execution_receipt  # 執行收據已產生
    assert reg.running_count() == 0


def test_idempotency_returns_existing(tmp_path):
    reg = TaskLifecycleRegistry(tmp_path / "tc.json")
    first = reg.create("chat::infer", idempotency_key="k-same")
    dup = reg.create("chat::infer", idempotency_key="k-same")
    assert dup.ok is False
    assert dup.reason == "duplicate-idempotency-key"
    assert dup.record["operation_id"] == first.record["operation_id"]
    # get_by_idempotency 對應同一 operation
    assert reg.get_by_idempotency("k-same").operation_id == first.record[
        "operation_id"
    ]


def test_operation_id_survives_backend_update(tmp_path):
    """Operation ID 跨 Backend 更新（世代提升）必須保留（§10.16 核心）。"""
    path = tmp_path / "tc.json"
    reg = TaskLifecycleRegistry(path)
    created = reg.create("rag::index", idempotency_key="k-gen")
    op = created.record["operation_id"]
    _to_running(reg, op, generation="gen-41")

    # 模擬 Backend 更新：新世代載入同一登錄簿
    reg2 = TaskLifecycleRegistry(path)
    loaded = reg2.get(op)
    assert loaded is not None
    assert loaded.operation_id == op
    assert loaded.backend_generation == "gen-41"
    assert loaded.execution_receipt  # 執行收據留存且跨載入可讀


def test_interrupt_and_verify_resume_flags(tmp_path):
    reg = TaskLifecycleRegistry(tmp_path / "tc.json")
    created = reg.create("git::commit", idempotency_key="k-int")
    op = created.record["operation_id"]
    _to_running(reg, op, generation="gen-41")

    assert reg.interrupt(op, error="backend-crashed").ok
    assert reg.get(op).status == INTERRUPTED
    assert reg.get(op).last_error == "backend-crashed"

    # 驗證為安全 → 可恢復（RECOVERING → 再 RUNNING）
    assert reg.verify_and_resume(op, safe=True).ok
    assert reg.get(op).status == RECOVERING
    resumed = reg.start(op, backend_generation="gen-42")
    assert resumed.ok, resumed.reason
    assert reg.get(op).attempt_count == 2

    # 驗證為不安全 → 必須終止，不得重跑
    created2 = reg.create("git::push", idempotency_key="k-int2")
    op2 = created2.record["operation_id"]
    _to_running(reg, op2, generation="gen-41")
    reg.interrupt(op2, error="backend-crashed")
    denied = reg.verify_and_resume(op2, safe=False, verified_error="unknown")
    assert denied.ok
    assert reg.get(op2).status == FAILED
    assert reg.get(op2).last_error == "unknown"


def test_verify_and_resume_requires_interrupted(tmp_path):
    reg = TaskLifecycleRegistry(tmp_path / "tc.json")
    created = reg.create("chat::infer", idempotency_key="k-v")
    op = created.record["operation_id"]
    result = reg.verify_and_resume(op, safe=True)
    assert result.ok is False
    assert result.reason == "not-interrupted:CREATED"


def test_illegal_transition_fails_closed(tmp_path):
    reg = TaskLifecycleRegistry(tmp_path / "tc.json")
    created = reg.create("ai::invoke", idempotency_key="k-ill")
    op = created.record["operation_id"]
    # CREATED 直接跳 COMPLETED 非法
    bad = reg.complete(op)
    assert bad.ok is False
    assert bad.reason == "illegal-transition:CREATED->COMPLETED"
    assert reg.get(op).status == CREATED
    # 終止後不得回到 RUNNING
    _to_running(reg, op)
    reg.complete(op)
    create_again = reg.create("ai::invoke", idempotency_key="k-ill")
    assert create_again.ok is False  # idempotency 鍵已鎖定不可重跑


def test_fail_records_error_and_persists(tmp_path):
    path = tmp_path / "tc.json"
    reg = TaskLifecycleRegistry(path)
    created = reg.create("file-sorter::sort", idempotency_key="k-fail")
    op = created.record["operation_id"]
    _to_running(reg, op)
    assert reg.fail(op, error="boom").ok
    assert reg.get(op).status == FAILED
    assert reg.get(op).last_error == "boom"

    reloaded = TaskLifecycleRegistry(path)
    assert reloaded.get(op).status == FAILED
    assert reloaded.get(op).last_error == "boom"


def test_snapshot_reports_counts(tmp_path):
    reg = TaskLifecycleRegistry(tmp_path / "tc.json")
    snap = reg.snapshot()
    assert snap["task_lifecycle_version"] == TASK_LIFECYCLE_VERSION
    assert snap["count"] == 0 and snap["running"] == 0

    created = reg.create("chat::infer", idempotency_key="k-snap")
    _to_running(reg, created.record["operation_id"])
    assert reg.snapshot()["count"] == 1
    assert reg.snapshot()["running"] == 1