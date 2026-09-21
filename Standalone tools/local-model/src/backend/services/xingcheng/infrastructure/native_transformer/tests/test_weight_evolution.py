"""Phase 7B — 權重演進機制端到端演練（藍圖 §2.3）。

五階段物件（Candidate → Training Checkpoint → Evaluation Result →
Verified → Active）與八步流程演練：訓練失敗保留現行、閘門拒收不啟用、
回滾到已認證版本、不得回復未認證舊權重、非法狀態遷移被拒。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

import pytest

from native_transformer.lifecycle import ModelLifecycle


def _fake_artifact(path: Path, tag: str) -> Path:
    path.write_text(f"artifact:{tag}", encoding="utf-8")
    return path


@pytest.fixture()
def lifecycle(tmp_path: Path) -> ModelLifecycle:
    lc = ModelLifecycle(model_id="test-line")
    lc.transition("INITIALIZED", reason="drill")
    return lc


def _promote(lc: ModelLifecycle, tmp_path: Path, tag: str, *, gate_pass: bool):
    """八步流程：候選→訓練→存權重→完整性→評估→閘門→啟用。"""
    # 1-2. 候選 + 訓練（此處以假檔代替真實訓練產物）
    weights = _fake_artifact(tmp_path / f"{tag}.pt", tag)
    # 3. 儲存候選權重（註冊但不啟用）
    entry = lc.register_artifact("weights", weights, activate=False)
    # 4. 完整性：sha256 已記錄
    assert len(entry["sha256"]) == 64
    # 5. 能力評估 → 評估報告 artefact
    report = _fake_artifact(
        tmp_path / f"eval-{tag}.json", json.dumps({"passed": gate_pass})
    )
    lc.register_artifact(
        "evaluation_report", report,
        metadata={"for_weights_version": entry["version"], "passed": gate_pass},
    )
    # 6-7. 發布閘門：不通過 → 不啟用（fail-closed）
    if not gate_pass:
        return entry
    # 8. 正式啟用
    lc.active_weights_version = int(entry["version"])
    return entry


def test_full_evolution_cycle(lifecycle: ModelLifecycle, tmp_path: Path) -> None:
    v1 = _promote(lifecycle, tmp_path, "v1", gate_pass=True)
    assert lifecycle.active_weights_version == v1["version"]
    v2 = _promote(lifecycle, tmp_path, "v2", gate_pass=True)
    assert lifecycle.active_weights_version == v2["version"]
    # 版本不覆蓋：兩個版本並存
    versions = lifecycle.artifacts["weights"]["versions"]
    assert len(versions) == 2


def test_gate_rejection_does_not_activate(lifecycle: ModelLifecycle, tmp_path: Path) -> None:
    v1 = _promote(lifecycle, tmp_path, "good", gate_pass=True)
    bad = _promote(lifecycle, tmp_path, "bad", gate_pass=False)
    assert lifecycle.active_weights_version == v1["version"]
    assert lifecycle.active_weights_version != bad["version"]
    active = lifecycle.active_weights()
    assert active is not None and "good" in active["path"]


def test_training_failure_preserves_current(lifecycle: ModelLifecycle, tmp_path: Path) -> None:
    v1 = _promote(lifecycle, tmp_path, "stable", gate_pass=True)
    lifecycle.transition("SFT_TRAINING", reason="next candidate")
    lifecycle.fail("loss diverged")
    # 訓練失敗：現行權重不變
    assert lifecycle.active_weights_version == v1["version"]
    # FAILED 只能回到 INITIALIZED 重來
    lifecycle.transition("INITIALIZED", reason="retry")


def test_rollback_only_to_registered(lifecycle: ModelLifecycle, tmp_path: Path) -> None:
    v1 = _promote(lifecycle, tmp_path, "r1", gate_pass=True)
    v2 = _promote(lifecycle, tmp_path, "r2", gate_pass=True)
    entry = lifecycle.rollback_weights(v1["version"])
    assert lifecycle.active_weights_version == v1["version"]
    assert entry["version"] == v1["version"]
    # 不得回復未認證版本：未註冊的版本號必須失敗
    with pytest.raises(ValueError, match="WEIGHTS_VERSION_UNKNOWN"):
        lifecycle.rollback_weights(99)


def test_missing_artifact_rejected(lifecycle: ModelLifecycle, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="ARTIFACT_MISSING"):
        lifecycle.register_artifact("weights", tmp_path / "ghost.pt")


def test_unknown_artifact_kind_rejected(lifecycle: ModelLifecycle, tmp_path: Path) -> None:
    f = _fake_artifact(tmp_path / "x.bin", "x")
    with pytest.raises(ValueError, match="ARTIFACT_KIND_UNKNOWN"):
        lifecycle.register_artifact("mystery", f)


def test_illegal_state_transition_denied(lifecycle: ModelLifecycle) -> None:
    with pytest.raises(ValueError, match="LIFECYCLE_TRANSITION_DENIED"):
        lifecycle.transition("READY")  # INITIALIZED → READY 非法
    with pytest.raises(ValueError, match="LIFECYCLE_STATE_UNKNOWN"):
        lifecycle.transition("SUPERPOWER")


def test_persist_roundtrip(lifecycle: ModelLifecycle, tmp_path: Path) -> None:
    _promote(lifecycle, tmp_path, "persisted", gate_pass=True)
    directory = tmp_path / "lc"
    directory.mkdir()
    lifecycle.save(directory)
    loaded = ModelLifecycle.load(directory)
    assert loaded.active_weights_version == lifecycle.active_weights_version
    assert loaded.artifacts["weights"]["versions"][0]["sha256"] == (
        lifecycle.artifacts["weights"]["versions"][0]["sha256"]
    )
    assert len(loaded.history) == len(lifecycle.history)


def test_tampered_lifecycle_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "lifecycle.json").write_text(
        json.dumps({"format": "wrong-format", "model_id": "x"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="LIFECYCLE_FORMAT_UNSUPPORTED"):
        ModelLifecycle.load(directory)
