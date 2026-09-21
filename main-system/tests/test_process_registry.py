"""§10.10 Process Registry tests — durable record + ownership boundary."""

from __future__ import annotations

import json

from core_system.process_registry import ProcessRegistry, REGISTRY_VERSION


def test_register_persists_and_reloads(tmp_path):
    path = tmp_path / "process-registry.json"
    reg = ProcessRegistry(path)
    rec = reg.register(4321, module_id="local-model", request_id="req-1")
    assert rec.pid == 4321
    assert rec.health == "running"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["registry_version"] == REGISTRY_VERSION
    assert data["processes"][0]["pid"] == 4321

    reloaded = ProcessRegistry(path)
    assert reloaded.get(4321).module_id == "local-model"


def test_ownership_boundary(tmp_path):
    reg = ProcessRegistry(tmp_path / "r.json")
    reg.register(1001, module_id="tool-a", owned=True)
    assert reg.is_owned(1001) is True
    assert reg.is_owned(9999) is False  # 未登錄 → 非本系統程序
    reg.register(1002, module_id="external-ollama", owned=False)
    assert reg.is_owned(1002) is False  # 共享服務非本系統啟動


def test_exited_process_not_owned(tmp_path):
    reg = ProcessRegistry(tmp_path / "r.json")
    reg.register(2001, module_id="tool-b")
    reg.mark_shutdown(2001, "exited")
    assert reg.is_owned(2001) is False


def test_released_process_still_owned(tmp_path):
    """放手追蹤但仍存活的本系統子程序仍是 owned（可終止）。"""
    reg = ProcessRegistry(tmp_path / "r.json")
    reg.register(2002, module_id="tool-c")
    reg.mark_shutdown(2002, "released")
    assert reg.is_owned(2002) is True
    assert reg.get(2002).health == "detached"


def test_restart_count_increments(tmp_path):
    reg = ProcessRegistry(tmp_path / "r.json")
    reg.register(3001, module_id="tool-d")
    reg.mark_shutdown(3001, "exited")
    rec = reg.register(3001, module_id="tool-d")  # same pid recycled
    assert rec.restart_count == 1
    assert rec.shutdown_state == ""


def test_reconcile_marks_dead_pids(tmp_path):
    reg = ProcessRegistry(tmp_path / "r.json")
    reg.register(99999999, module_id="ghost")  # 不存在的 PID
    stats = reg.reconcile()
    assert stats["marked_exited"] == 1
    assert reg.get(99999999).shutdown_state == "exited"
    assert reg.is_owned(99999999) is False


def test_snapshot_counts_active(tmp_path):
    reg = ProcessRegistry(tmp_path / "r.json")
    reg.register(4001, module_id="a")
    reg.register(4002, module_id="b")
    reg.mark_shutdown(4002, "exited")
    snap = reg.snapshot()
    assert snap["active"] == 1
    assert len(snap["processes"]) == 2


def test_mark_health(tmp_path):
    reg = ProcessRegistry(tmp_path / "r.json")
    reg.register(5001, module_id="c")
    reg.mark_health(5001, "degraded")
    assert reg.get(5001).health == "degraded"
