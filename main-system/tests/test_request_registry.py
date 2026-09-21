"""§10.22 Request Registry tests — 14-field backward-compatible extension."""

from __future__ import annotations

import json

from core_system.request_registry import (
    REQUEST_FIELDS,
    REQUEST_REGISTRY_VERSION,
    RequestRegistry,
)


def test_fourteen_fields_present():
    assert "request_id" in REQUEST_FIELDS
    assert "session_id" in REQUEST_FIELDS
    assert "task_id" in REQUEST_FIELDS
    assert "backend_id" in REQUEST_FIELDS
    assert "backend_generation" in REQUEST_FIELDS
    assert "release_id" in REQUEST_FIELDS
    assert "method" in REQUEST_FIELDS
    assert "created_at" in REQUEST_FIELDS
    assert "started_at" in REQUEST_FIELDS
    assert "completed_at" in REQUEST_FIELDS
    assert "status" in REQUEST_FIELDS
    assert "timeout" in REQUEST_FIELDS
    assert "cancellation_state" in REQUEST_FIELDS
    assert "error_code" in REQUEST_FIELDS


def test_upsert_and_persistence(tmp_path):
    path = tmp_path / "requests.json"
    reg = RequestRegistry(path)
    result = reg.upsert(
        "req-1",
        session_id="sess-1",
        method="ai::infer",
        backend_generation="gen-41",
    )
    assert result.ok
    record = reg.get("req-1")
    assert record.session_id == "sess-1"
    assert record.method == "ai::infer"

    reloaded = RequestRegistry(path)
    record2 = reloaded.get("req-1")
    assert record2 is not None
    assert record2.backend_generation == "gen-41"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["request_registry_version"] == REQUEST_REGISTRY_VERSION


def test_legacy_cancelled_bool_maps_to_cancellation_state(tmp_path):
    """既有 UI/IPC 的 cancelled 布林對映為 cancellation_state。"""
    reg = RequestRegistry(tmp_path / "requests.json")
    reg.upsert("req-1", method="tool::run", cancelled=True)
    assert reg.get("req-1").cancellation_state == "cancelled"
    reg.upsert("req-2", method="tool::run", cancelled=False)
    assert reg.get("req-2").cancellation_state == ""


def test_backward_compatible_old_format_only_keys(tmp_path):
    """只帶 request_id/method 的舊格式 payload 不會崩潰。"""
    reg = RequestRegistry(tmp_path / "requests.json")
    result = reg.upsert("req-old")
    assert result.ok
    assert reg.get("req-old").method == ""


def test_lifecycle_marks_tracked(tmp_path):
    reg = RequestRegistry(tmp_path / "requests.json")
    reg.upsert("req-1", method="ai::infer", backend_generation="gen-41")
    assert reg.mark_started("req-1").ok
    assert reg.get("req-1").status == "RUNNING"
    assert reg.mark_completed("req-1").ok
    assert reg.get("req-1").status == "COMPLETED"
    assert reg.get("req-1").completed_at


def test_failed_and_timed_out_set_error_done(tmp_path):
    reg = RequestRegistry(tmp_path / "requests.json")
    reg.upsert("req-1")
    reg.mark_started("req-1")
    reg.mark_failed("req-1", error_code="E101")
    assert reg.get("req-1").status == "FAILED"
    assert reg.get("req-1").error_code == "E101"

    reg.upsert("req-2")
    reg.mark_started("req-2")
    reg.mark_timed_out("req-2")
    assert reg.get("req-2").status == "TIMED_OUT"


def test_cancellation_flow(tmp_path):
    reg = RequestRegistry(tmp_path / "requests.json")
    reg.upsert("req-1")
    reg.mark_started("req-1")
    assert reg.request_cancel("req-1").ok
    assert reg.get("req-1").cancellation_state == "requested"
    assert reg.mark_cancelled("req-1").ok
    assert reg.get("req-1").status == "CANCELLED"
    assert reg.get("req-1").cancellation_state == "cancelled"


def test_update_missing_request_fails_closed(tmp_path):
    reg = RequestRegistry(tmp_path / "requests.json")
    result = reg.update("nope", status="RUNNING")
    assert result.ok is False
    assert result.reason == "request-not-found"


def test_in_flight_and_generation_queries(tmp_path):
    reg = RequestRegistry(tmp_path / "requests.json")
    reg.upsert("req-1", backend_generation="gen-41")
    reg.mark_started("req-1")
    reg.upsert("req-2", backend_generation="gen-41")
    reg.upsert("req-3", backend_generation="gen-42")
    reg.mark_started("req-3")
    reg.mark_completed("req-3")

    in_flight = {r.request_id for r in reg.in_flight()}
    assert "req-1" in in_flight
    assert "req-2" in in_flight
    assert "req-3" not in in_flight

    gen41 = {r.request_id for r in reg.by_backend_generation("gen-41")}
    assert gen41 == {"req-1", "req-2"}


def test_snapshot_reports_fields_and_count(tmp_path):
    reg = RequestRegistry(tmp_path / "requests.json")
    reg.upsert("req-1", method="ai::infer")
    snap = reg.snapshot()
    assert snap["request_registry_version"] == REQUEST_REGISTRY_VERSION
    assert len(snap["fields"]) == 14
    assert snap["count"] == 1