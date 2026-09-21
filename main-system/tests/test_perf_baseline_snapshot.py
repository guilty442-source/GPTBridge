"""§10.11 performance baseline collector tests."""

from __future__ import annotations

import json

from tasks.perf_baseline_snapshot import (
    BASELINE_VERSION,
    collect_baseline,
    persist_baseline,
)


def test_collect_returns_all_sections(tmp_path):
    snap = collect_baseline(tmp_path)
    assert snap["baseline_version"] == BASELINE_VERSION
    assert "captured_at" in snap
    assert "process" in snap
    assert "git" in snap
    assert "process_registry" in snap
    # 未注入的量測標 None，不造假
    assert snap["ipc"] is None
    assert snap["rag"] is None


def test_extra_injection(tmp_path):
    snap = collect_baseline(
        tmp_path, extra={"ipc": {"p95_ms": 12.0}, "boot_to_ready_s": 4.2}
    )
    assert snap["extra"]["ipc"]["p95_ms"] == 12.0
    assert snap["extra"]["boot_to_ready_s"] == 4.2


def test_persist_writes_snapshot_and_latest(tmp_path):
    snap = collect_baseline(tmp_path)
    target = persist_baseline(tmp_path, snap)
    assert target.exists()
    latest = tmp_path / "main-system" / "runtime" / "state" / "perf-baseline-latest.json"
    assert latest.exists()
    data = json.loads(latest.read_text(encoding="utf-8"))
    assert data["captured_at"] == snap["captured_at"]


def test_git_timing_on_real_repo():
    from pathlib import Path

    snap = collect_baseline(Path(__file__).resolve().parents[2])
    # 在真實 repo 上 git status 應可量測；非 git 目錄則為 None
    assert snap["git"]["status_ms"] is None or snap["git"]["status_ms"] >= 0
