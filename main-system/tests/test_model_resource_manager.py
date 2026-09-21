"""§10.7 Model Resource Manager tests — role policy + resource gate + ledger."""

from __future__ import annotations

import json

from core_system.model_resource_manager import (
    ModelResourceManager,
    ModelRole,
    Retention,
    DEFAULT_POLICIES,
)


def _manager(tmp_path, gpu_free=6000.0, ram_free=16000.0):
    return ModelResourceManager(
        ledger_path=tmp_path / "ledger.jsonl",
        gpu_free_fn=lambda: gpu_free,
        ram_free_fn=lambda: ram_free,
        interactive_headroom_mb=1024,
    )


def test_role_policies_match_blueprint():
    assert DEFAULT_POLICIES[ModelRole.XINGCHENG_NATIVE].retention == Retention.RESIDENT
    assert DEFAULT_POLICIES[ModelRole.FAST_CHAT].retention == Retention.RESIDENT
    assert DEFAULT_POLICIES[ModelRole.CODING_LARGE].retention == Retention.ON_DEMAND
    assert DEFAULT_POLICIES[ModelRole.EMBEDDING].retention == Retention.ON_DEMAND
    assert DEFAULT_POLICIES[ModelRole.RERANKER].retention == Retention.ON_DEMAND
    assert DEFAULT_POLICIES[ModelRole.DEEP_REASONING].retention == Retention.SCHEDULED
    assert DEFAULT_POLICIES[ModelRole.XINGCHENG_NATIVE].interactive is True


def test_admit_within_budget(tmp_path):
    mgr = _manager(tmp_path)
    d = mgr.request_load(ModelRole.FAST_CHAT, "chat-8b", vram_mb=3000, ram_mb=6000)
    assert d.admitted
    assert mgr.loaded()[0].model_id == "chat-8b"


def test_large_coding_respects_interactive_headroom(tmp_path):
    """大型 Coding 載入不得吃掉互動保留區。"""
    mgr = _manager(tmp_path, gpu_free=5000.0)
    d = mgr.request_load(ModelRole.CODING_LARGE, "coding-30b", vram_mb=4500)
    assert not d.admitted
    assert "headroom" in d.reason
    assert not mgr.loaded()


def test_interactive_role_exempt_from_headroom(tmp_path):
    mgr = _manager(tmp_path, gpu_free=5000.0)
    d = mgr.request_load(ModelRole.FAST_CHAT, "chat-8b", vram_mb=4500)
    assert d.admitted  # 互動角色不需留 headroom


def test_fail_closed_when_telemetry_missing(tmp_path):
    mgr = ModelResourceManager(ledger_path=tmp_path / "l.jsonl")  # no probes
    d = mgr.request_load(ModelRole.CODING_LARGE, "coding-30b", vram_mb=4000)
    assert not d.admitted
    assert d.reason == "gpu-telemetry-unavailable"


def test_release(tmp_path):
    mgr = _manager(tmp_path)
    mgr.request_load(ModelRole.EMBEDDING, "emb", vram_mb=500)
    assert mgr.release("emb") is True
    assert not mgr.loaded()
    assert mgr.release("emb") is False


def test_measurement_ledger_appends(tmp_path):
    mgr = _manager(tmp_path)
    mgr.record_measurement(
        "chat-8b",
        ModelRole.FAST_CHAT,
        {"load_time_s": 2.1, "ttft_ms": 340, "tokens_per_s": 42.0},
    )
    mgr.record_measurement(
        "coding-30b", ModelRole.CODING_LARGE, {"load_time_s": 18.7}
    )
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    entry = json.loads(lines[0])
    assert entry["role"] == "fast_chat"
    assert entry["metrics"]["ttft_ms"] == 340
