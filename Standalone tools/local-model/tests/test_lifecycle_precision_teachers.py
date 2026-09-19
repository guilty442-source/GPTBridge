# -*- coding: utf-8 -*-
"""Phase 4/6/8/10 缺口件：生命週期狀態機、精度策略、教師角色、SDPA 驗證、語料治理欄位。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "src"
        / "backend"
        / "services"
        / "xingcheng"
        / "infrastructure"
    ),
)

import torch  # noqa: E402

from native_transformer.lifecycle import (  # noqa: E402
    LIFECYCLE_FORMAT,
    STATES,
    ModelLifecycle,
)
from native_transformer.training.precision import resolve_precision  # noqa: E402
from native_transformer.training.teachers import (  # noqa: E402
    assign_teachers,
    assert_loopback_endpoint,
    resolve_teacher_model,
    teacher_role,
)
from native_transformer.execution.backend import describe_sdpa_backends  # noqa: E402
from native_transformer.training.corpus import build_corpus  # noqa: E402


# ── 生命週期 ──────────────────────────────────────────────────
def test_lifecycle_full_path(tmp_path):
    lc = ModelLifecycle("test-model")
    assert lc.state == "UNINITIALIZED"
    lc.transition("INITIALIZED")
    lc.transition("PRETRAINING")
    lc.transition("PRETRAINED")
    lc.transition("SFT_TRAINING")
    lc.transition("INSTRUCT_READY")
    lc.transition("EVALUATING")
    lc.transition("READY")
    lc.transition("LOADED")
    lc.transition("UNLOADED")
    assert lc.state == "UNLOADED"
    assert len(lc.history) == 9


def test_lifecycle_illegal_transition_denied():
    lc = ModelLifecycle("m")
    with pytest.raises(ValueError, match="LIFECYCLE_TRANSITION_DENIED"):
        lc.transition("READY")
    with pytest.raises(ValueError, match="LIFECYCLE_STATE_UNKNOWN"):
        lc.transition("BOGUS")


def test_lifecycle_failed_recovery():
    lc = ModelLifecycle("m")
    lc.transition("INITIALIZED")
    lc.transition("PRETRAINING")
    lc.fail("loss exploded")
    assert lc.state == "FAILED"
    lc.transition("INITIALIZED")
    assert lc.state == "INITIALIZED"


def test_lifecycle_artifact_versions_no_overwrite(tmp_path):
    lc = ModelLifecycle("m")
    w1 = tmp_path / "v1.pt"
    w2 = tmp_path / "v2.pt"
    w1.write_bytes(b"weights-one")
    w2.write_bytes(b"weights-two")
    e1 = lc.register_artifact("weights", w1, activate=True)
    e2 = lc.register_artifact("weights", w2, activate=True)
    assert e1["version"] == 1 and e2["version"] == 2
    assert e1["sha256"] != e2["sha256"]
    assert lc.active_weights()["version"] == 2
    lc.rollback_weights(1)
    assert lc.active_weights()["version"] == 1
    # 版本紀錄不消失
    assert len(lc.artifacts["weights"]["versions"]) == 2
    with pytest.raises(ValueError, match="WEIGHTS_VERSION_UNKNOWN"):
        lc.rollback_weights(99)


def test_lifecycle_persistence_roundtrip(tmp_path):
    lc = ModelLifecycle("m")
    lc.transition("INITIALIZED", reason="boot")
    w = tmp_path / "w.pt"
    w.write_bytes(b"x")
    lc.register_artifact("weights", w, activate=True)
    lc.save(tmp_path)
    loaded = ModelLifecycle.load(tmp_path)
    assert loaded.state == "INITIALIZED"
    assert loaded.active_weights()["sha256"] == lc.active_weights()["sha256"]
    reloaded = ModelLifecycle.load_or_create(tmp_path, "other")
    assert reloaded.model_id == "m"
    fresh = ModelLifecycle.load_or_create(tmp_path / "empty", "new")
    assert fresh.state == "UNINITIALIZED"


def test_lifecycle_artifact_missing_file_denied(tmp_path):
    lc = ModelLifecycle("m")
    with pytest.raises(FileNotFoundError, match="ARTIFACT_MISSING"):
        lc.register_artifact("weights", tmp_path / "nope.pt")


# ── 精度策略 ──────────────────────────────────────────────────
def test_precision_auto_cpu_is_fp32():
    plan = resolve_precision(torch.device("cpu"), "auto")
    assert plan.name == "fp32" and plan.dtype is None and not plan.enabled
    assert plan.scaler() is None
    with plan.autocast():
        pass


def test_precision_explicit_requests():
    bf16 = resolve_precision("cpu", "bf16")
    assert bf16.dtype == torch.bfloat16 and bf16.enabled
    # CPU 上 fp16 不受支援 → fail-closed 回 fp32
    fp16_cpu = resolve_precision("cpu", "fp16")
    assert fp16_cpu.name == "fp32" and fp16_cpu.dtype is None
    fp32 = resolve_precision("cuda", "fp32")
    assert fp32.dtype is None


def test_precision_unknown_rejected():
    with pytest.raises(ValueError, match="PRECISION_UNKNOWN"):
        resolve_precision("cpu", "tf32")


# ── 教師角色 ──────────────────────────────────────────────────
def test_teacher_roles_defined():
    assert teacher_role("language").preferred_families[0] == "qwen"
    assert teacher_role("reasoning").preferred_families[0] == "deepseek"
    with pytest.raises(ValueError, match="TEACHER_ROLE_UNKNOWN"):
        teacher_role("spy")


def test_resolve_teacher_prefers_family():
    models = ["gemma4:e2b-it-qat", "deepseek-r1:7b", "qwen2.5:14b"]
    assert resolve_teacher_model("reasoning", models) == "deepseek-r1:7b"
    assert resolve_teacher_model("language", models) == "qwen2.5:14b"
    assert resolve_teacher_model("coding", ["gemma4:e2b-it-qat"]) is None


def test_assign_teachers_with_override():
    assigned = assign_teachers(
        ["qwen2.5:14b"], overrides={"coding": "custom-code:7b"}
    )
    assert assigned["language"] == "qwen2.5:14b"
    assert assigned["coding"] == "custom-code:7b"
    assert assigned["reasoning"] == "qwen2.5:14b"  # fallback 到次選 family


def test_loopback_endpoint_guard():
    assert assert_loopback_endpoint("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert assert_loopback_endpoint("http://localhost:11434")
    assert assert_loopback_endpoint("http://[::1]:11434")
    for bad in ("http://8.8.8.8:11434", "http://192.168.1.1:11434", "ftp://x"):
        with pytest.raises(ValueError, match="TEACHER_ENDPOINT_FORBIDDEN"):
            assert_loopback_endpoint(bad)


# ── SDPA 後端驗證 ─────────────────────────────────────────────
def test_sdpa_backend_report():
    report = describe_sdpa_backends()
    assert report["device"] in ("cpu", "cuda")
    assert "flags" in report and "expected_backend" in report
    if report["device"] == "cpu":
        assert report["flash_active"] is False
        assert report["expected_backend"] == "math"


# ── 語料治理欄位 ──────────────────────────────────────────────
def test_corpus_manifest_governance_fields(tmp_path):
    docs = tmp_path / "src"
    docs.mkdir()
    (docs / "a.md").write_text("# 文件\n內容 " * 50, encoding="utf-8")
    (docs / "b.py").write_text("def f():\n    return 1\n" * 30, encoding="utf-8")
    out = tmp_path / "corpus"
    manifest = build_corpus(
        docs, out, sources=("",), suffixes=(".md", ".py"), val_permille=0
    )
    assert manifest["dataset_format"] == "star-corpus/v1"
    assert manifest["dataset_id"].startswith("star-corpus-")
    assert manifest["license"] == "first-party-internal"
    assert manifest["language"]
    assert manifest["train"]["sha256"]
    on_disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["dataset_id"] == manifest["dataset_id"]


def test_corpus_manifest_token_count_with_tokenizer(tmp_path):
    from native_transformer.tokenizer import XingChengTokenizer

    docs = tmp_path / "src"
    docs.mkdir()
    (docs / "a.txt").write_text("一些文字 " * 20, encoding="utf-8")
    out = tmp_path / "corpus"
    tok = XingChengTokenizer(vocab_size=300)
    manifest = build_corpus(
        docs, out, sources=("",), suffixes=(".txt",), val_permille=0, tokenizer=tok
    )
    assert manifest["token_count"] > 0
