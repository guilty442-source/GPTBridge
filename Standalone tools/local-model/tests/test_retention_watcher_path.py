"""P1-7：CLI／watch 路徑必須涵蓋 apply_retention（§10.67）。"""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import threading
from pathlib import Path

import pytest

from xingcheng.infrastructure.native_transformer import (
    self_learning_support as sls,
)

AUDIT = Path("xingcheng") / "runtime" / "logs" / "retention.jsonl"


def test_run_cycle_attaches_retention_and_writes_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_cycle（--run-once／--watch 共用入口）結尾執行 apply_retention。"""
    monkeypatch.setattr(
        sls,
        "run_cycle_impl",
        lambda *a, **k: {"ok": True, "action": "skipped"},
    )
    result = sls.run_cycle(tmp_path)
    assert result["ok"]
    assert result["retention"]["ok"] is True
    assert (tmp_path / AUDIT).is_file()


def test_run_cycle_disabled_skips_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kill-switch（action=disabled）時 fail-closed，連清理都不做。"""
    monkeypatch.setattr(
        sls,
        "run_cycle_impl",
        lambda *a, **k: {"ok": True, "action": "disabled"},
    )
    result = sls.run_cycle(tmp_path)
    assert result["action"] == "disabled"
    assert "retention" not in result
    assert not (tmp_path / AUDIT).exists()


def test_public_run_cycle_delegates_to_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """self_learning.run_cycle 與 CLI 路徑共用同一 retention 入口。"""
    from xingcheng.infrastructure.native_transformer import self_learning

    monkeypatch.setattr(
        sls,
        "run_cycle_impl",
        lambda *a, **k: {"ok": True, "action": "skipped"},
    )
    result = self_learning.run_cycle(tmp_path)
    assert result["retention"]["ok"] is True


def test_watch_loop_default_cycle_uses_run_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--watch 的預設 cycle_fn 走 run_cycle（含 retention），非裸 impl。"""
    called: list[dict] = []
    stop = threading.Event()

    def fake_run_cycle(tool, **kwargs):
        called.append({"tool": str(tool), **kwargs})
        stop.set()
        return {"ok": True, "retention": {"ok": True}}

    monkeypatch.setattr(sls, "run_cycle", fake_run_cycle)
    rc = sls._watch_loop(
        tmp_path,
        interval=60.0,
        emit=lambda e: None,
        enabled_fn=lambda: True,
        stop_event=stop,
    )
    assert rc == 0
    assert called and called[0]["tool"] == str(tmp_path)
