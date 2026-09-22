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


def test_shared_manager_factory_singleton(monkeypatch):
    """§10.7: get_model_resource_manager 共享實例 + lazy 遙測。"""
    import core_system.model_resource_manager as mod

    monkeypatch.setattr(mod, "_SHARED", None)
    mgr = mod.get_model_resource_manager()
    assert mgr is mod.get_model_resource_manager()
    assert mgr._ledger_path.name == "model-resource-ledger.jsonl"


def test_embedding_load_gated_by_resource_manager(monkeypatch):
    """§10.7 漸進遷移：LocalEmbeddingProvider 載入前經 request_load；
    否決 fail-closed，載入失敗對稱 release。"""
    import sys
    import types

    from core_system.rag.embeddings import LocalEmbeddingProvider

    calls = {"requests": [], "releases": []}

    class _Mgr:
        def __init__(self, admit):
            self.admit = admit

        def request_load(self, role, model_id, *, vram_mb=0, ram_mb=0):
            calls["requests"].append((role, model_id, ram_mb))
            return type("D", (), {"admitted": self.admit, "reason": "x"})()

        def release(self, model_id):
            calls["releases"].append(model_id)
            return True

    import core_system.model_resource_manager as mod

    # 否決 → fail-closed，不觸碰 sentence_transformers
    monkeypatch.setattr(mod, "_SHARED", _Mgr(admit=False))
    provider = LocalEmbeddingProvider()
    try:
        provider._load_model()
    except RuntimeError as exc:
        assert "resource gate" in str(exc)
    else:
        raise AssertionError("denied load must fail closed")
    assert calls["requests"] and calls["requests"][0][2] == 512
    assert provider._model is None

    # 核准但載入拋錯 → 對稱 release
    calls["requests"].clear()
    monkeypatch.setattr(mod, "_SHARED", _Mgr(admit=True))
    fake_st = types.ModuleType("sentence_transformers")

    class _ST:
        def __init__(self, name):
            raise RuntimeError("load boom")

    fake_st.SentenceTransformer = _ST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    provider2 = LocalEmbeddingProvider()
    try:
        provider2._load_model()
    except RuntimeError as exc:
        assert "load boom" in str(exc)
    else:
        raise AssertionError("load failure must propagate")
    assert calls["releases"] == ["sentence-transformers/all-MiniLM-L6-v2"]
