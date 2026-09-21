"""G62 / §10.25 Request Registry 十項驗收（隔離環境，tmp_path）。

1  正常完成  2 並發多請求  3 取消  4 超時  5 重複取消冪等
6  完成／取消競態單一結果  7 斷線 INTERRUPTED＋恢復  8 串流事件歸屬
9  舊 IPC 格式相容  10 不繞過權限／稽核（結構性斷言）
"""

from __future__ import annotations

from pathlib import Path

from core_system.request_registry import RequestRegistry
from tasks.state_outbox_store import OutboxStore


def _registry(tmp_path: Path) -> RequestRegistry:
    return RequestRegistry(tmp_path / "requests.json")


def test_1_normal_request_completes(tmp_path):
    reg = _registry(tmp_path)
    reg.upsert("r1", method="chat.send")
    assert reg.get("r1").status == "CREATED"
    reg.update("r1", status="QUEUED")
    reg.mark_started("r1")
    result = reg.mark_completed("r1")
    assert result.ok and result.record["status"] == "COMPLETED"
    assert result.record["completed_at"]
    assert reg.in_flight() == []


def test_2_concurrent_requests_independent(tmp_path):
    reg = _registry(tmp_path)
    ids = [f"r{i}" for i in range(8)]
    for rid in ids:
        reg.upsert(rid, method="chat.send", backend_generation="gen-a")
    for rid in ids:
        reg.update(rid, status="QUEUED")
        reg.mark_started(rid)
    assert len(reg.in_flight()) == 8
    # interleaved outcomes
    reg.mark_completed("r0")
    reg.mark_failed("r1", error_code="E")
    reg.mark_cancelled("r2")
    reg.mark_timed_out("r3")
    assert len(reg.in_flight()) == 4
    assert reg.get("r0").status == "COMPLETED"
    assert reg.get("r1").status == "FAILED"
    assert reg.get("r2").status == "CANCELLED"
    assert reg.get("r3").status == "TIMED_OUT"
    for rid in ids[4:]:
        reg.mark_completed(rid)
    assert reg.in_flight() == []


def test_3_request_cancellation(tmp_path):
    reg = _registry(tmp_path)
    reg.upsert("r1", method="chat.send")
    reg.update("r1", status="QUEUED")
    reg.request_cancel("r1")
    assert reg.get("r1").cancellation_state == "requested"
    reg.mark_cancelled("r1")
    rec = reg.get("r1")
    assert rec.status == "CANCELLED" and rec.cancellation_state == "cancelled"


def test_4_request_timeout(tmp_path):
    reg = _registry(tmp_path)
    reg.upsert("r1", method="chat.send")
    reg.update("r1", status="QUEUED")
    reg.mark_started("r1")
    result = reg.mark_timed_out("r1")
    assert result.ok and result.record["status"] == "TIMED_OUT"


def test_5_duplicate_cancellation_idempotent(tmp_path):
    reg = _registry(tmp_path)
    reg.upsert("r1", method="chat.send")
    reg.update("r1", status="QUEUED")
    first = reg.mark_cancelled("r1")
    assert first.ok
    # duplicate cancel is idempotent: same-state update returns existing
    # record unchanged — no second transition, no second side effect
    second = reg.mark_cancelled("r1")
    assert second.ok and second.record["status"] == "CANCELLED"
    # but a *conflicting* late outcome is still rejected fail-closed
    assert not reg.mark_completed("r1").ok
    assert reg.get("r1").status == "CANCELLED"


def test_6_complete_and_cancel_race_single_result(tmp_path):
    reg = _registry(tmp_path)
    reg.upsert("r1", method="chat.send")
    reg.update("r1", status="QUEUED")
    reg.mark_started("r1")
    # completion wins
    reg.mark_completed("r1")
    late_cancel = reg.mark_cancelled("r1")
    assert not late_cancel.ok and late_cancel.reason == "request-terminal"
    assert reg.get("r1").status == "COMPLETED"
    # and the reverse order
    reg.upsert("r2", method="chat.send")
    reg.update("r2", status="QUEUED")
    reg.mark_started("r2")
    reg.mark_cancelled("r2")
    late_complete = reg.mark_completed("r2")
    assert not late_complete.ok and late_complete.reason == "request-terminal"
    assert reg.get("r2").status == "CANCELLED"


def test_7_backend_disconnect_marks_interrupted_and_recovers(tmp_path):
    reg = _registry(tmp_path)
    reg.upsert("r1", method="chat.send", backend_generation="gen-a")
    reg.update("r1", status="QUEUED")
    reg.mark_started("r1")
    reg.mark_interrupted("r1", error_code="BACKEND_LOST")
    rec = reg.get("r1")
    assert rec.status == "INTERRUPTED" and rec.error_code == "BACKEND_LOST"
    # INTERRUPTED is not terminal: verified recovery paths
    reg.mark_started("r1")  # INTERRUPTED -> RUNNING allowed
    assert reg.get("r1").status == "RUNNING"
    reg.mark_interrupted("r1")
    reg.mark_failed("r1", error_code="UNRECOVERABLE")
    assert reg.get("r1").status == "FAILED"
    # terminal after recovery failure — no further transitions
    assert not reg.mark_started("r1").ok


def test_8_streaming_events_attributed_per_request(tmp_path):
    """Token/state events carry request correlation + monotonic sequence;
    per-generation idempotency keys prevent duplicate delivery."""
    store = OutboxStore(tmp_path / "outbox.sqlite3")
    events = []
    for seq_req in ("req-a", "req-b"):
        for i in range(3):
            events.append(store.append(
                entity_id=f"request:{seq_req}",
                entity_type="request",
                operation="token",
                backend_generation="gen-1",
                correlation_id=seq_req,
            ))
    # monotonic global sequence
    seqs = [e["sequence"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == 6
    # per-request attribution via correlation_id — no cross-talk
    by_req = {}
    for e in store.fetch_after(0):
        by_req.setdefault(e["correlation_id"], []).append(e["sequence"])
    assert set(by_req) == {"req-a", "req-b"}
    assert all(len(v) == 3 for v in by_req.values())
    # per-entity revision monotonic within each request stream
    assert [e["authoritative_revision"] for e in store.fetch_after(0)
            if e["correlation_id"] == "req-a"] == [1, 2, 3]


def test_9_legacy_ipc_format_still_works(tmp_path):
    reg = _registry(tmp_path)
    # legacy payload: command/status/cancelled only
    result = reg.upsert({
        "request_id": "legacy-1",
        "command": "chat.send",
        "status": "RUNNING",
        "cancelled": False,
    })
    assert result.ok
    rec = reg.get("legacy-1")
    assert rec.method == "chat.send" and rec.status == "RUNNING"
    reg.upsert({"request_id": "legacy-1", "cancelled": True})
    assert reg.get("legacy-1").cancellation_state == "cancelled"


def test_10_registry_is_tracking_only_no_auth_bypass(tmp_path):
    """Registry never grants capability: records carry identity fields only;
    updates cannot create requests out of thin air via update() (fail-closed)
    and cancellation_state alone does not change request status."""
    reg = _registry(tmp_path)
    assert not reg.update("ghost", status="RUNNING").ok
    reg.upsert("r1", method="chat.send")
    reg.update("r1", status="QUEUED")
    reg.request_cancel("r1")
    # requesting cancel does NOT cancel — status unchanged until marked
    assert reg.get("r1").status == "QUEUED"
    assert reg.get("r1").cancellation_state == "requested"
