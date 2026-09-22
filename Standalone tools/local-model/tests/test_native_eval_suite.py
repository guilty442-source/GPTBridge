"""native_eval_suite：套件載入、metrics 評估、閘門判定、寫入評估表。"""
import json
from pathlib import Path

import pytest

import _xingcheng_test_support  # noqa: F401  (sys.path 注入)

from xingcheng.infrastructure.native_eval_suite import (
    SUITE_FORMAT_VERSION,
    compare_metrics,
    evaluate_checkpoint,
    load_suite,
    run_evaluation,
)
from xingcheng.infrastructure.native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
    save_checkpoint,
)
from xingcheng.infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)
from test_phase3_sft_adapters import _completed_job, _tool


def _write_checkpoint(path: Path) -> None:
    config = XingChengConfig.small()
    config.vocab_size = 264
    config.max_position_embeddings = 64
    model = XingChengForCausalLM(config)
    tok = XingChengTokenizer.from_config(config)
    save_checkpoint(
        path, model, tokenizer=tok, config=config,
        metadata={"stage": "eval-test"},
    )


def _write_suite(path: Path, gates=None) -> Path:
    suite = {
        "format_version": SUITE_FORMAT_VERSION,
        "suite_id": "smoke-eval",
        "eval_text": "the quick brown fox jumps over the lazy dog",
        "sanity_prompt": "the quick",
        "sanity_max_new_tokens": 4,
        "seed": 7,
        "quality_gates": gates
        or {
            "max_perplexity_regression_pct": 50.0,
            "require_generation": True,
        },
        "baseline_metrics": {"perplexity": 200.0},
    }
    path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_suite_validates_and_hashes(tmp_path):
    suite_path = _write_suite(tmp_path / "suite.json")
    suite = load_suite(suite_path)
    assert suite["suite_id"] == "smoke-eval"
    assert len(suite["suite_sha256"]) == 64

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"suite_id": "x"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_suite(bad)


def test_compare_metrics_gates():
    gates = {"max_perplexity_regression_pct": 10.0, "require_generation": True}
    base = {"perplexity": 100.0}
    ok, passed = compare_metrics(
        base, {"perplexity": 105.0, "generation_ok": True}, gates
    )
    assert passed and ok["perplexity_delta_pct"] == pytest.approx(5.0)
    _, failed = compare_metrics(
        base, {"perplexity": 120.0, "generation_ok": True}, gates
    )
    assert not failed
    _, failed_gen = compare_metrics(
        base, {"perplexity": 100.0, "generation_ok": False}, gates
    )
    assert not failed_gen


def test_run_evaluation_records_and_validates(tmp_path):
    ckpt = tmp_path / "cand.pt"
    _write_checkpoint(ckpt)
    suite_path = _write_suite(tmp_path / "suite.json")
    repo = TransformerTrainingRepository(tmp_path)
    job = _completed_job(repo, tmp_path)
    # 候選 artifact 必須落在 tool_root 內
    cand = _tool(tmp_path) / "runtime/models/adapters/cand.pt"
    cand.parent.mkdir(parents=True, exist_ok=True)
    _write_checkpoint(cand)
    adapter = repo.register_adapter_candidate(
        job_id=str(job["job_id"]),
        artifact_path=str(cand),
        metrics={},
    )
    adapter_id = str(adapter["adapter_id"])
    result = run_evaluation(
        repo,
        adapter_id=adapter_id,
        candidate_checkpoint=cand,
        suite_path=suite_path,
    )
    assert result["ok"] and result["passed"]
    ev = result["evaluation"]
    assert ev["passed"] == 1
    assert ev["suite_sha256"] == result["suite_sha256"]
    # gate 通過 → candidate 升級為 validated
    with repo._connect() as connection:
        status = connection.execute(
            "SELECT status FROM transformer_adapter_candidate "
            "WHERE adapter_id = ?",
            (adapter_id,),
        ).fetchone()["status"]
    assert status == "validated"
    # 同套件重評被 UNIQUE 擋
    with pytest.raises(Exception):
        run_evaluation(
            repo,
            adapter_id=adapter_id,
            candidate_checkpoint=cand,
            suite_path=suite_path,
        )


def test_compare_metrics_tps_ratio_gate():
    gates = {"min_tps_baseline_ratio": 0.9}
    base = {"tokens_per_second": 10.0}
    _, ok = compare_metrics(
        base, {"generation_ok": True, "tokens_per_second": 9.0}, gates
    )
    assert ok
    cmp_, bad = compare_metrics(
        base, {"generation_ok": True, "tokens_per_second": 8.9}, gates
    )
    assert not bad
    assert cmp_["tokens_per_second_ratio_ok"] is False
    # baseline tps missing -> fail closed
    _, denied = compare_metrics(
        {}, {"generation_ok": True, "tokens_per_second": 99.0}, gates
    )
    assert not denied


def test_compare_metrics_tps_ratio_and_floor():
    gates = {"min_tokens_per_second": 5.0, "min_tps_baseline_ratio": 0.5}
    base = {"tokens_per_second": 20.0}
    _, ok = compare_metrics(
        base, {"generation_ok": True, "tokens_per_second": 10.0}, gates
    )
    assert ok
    _, bad = compare_metrics(
        base, {"generation_ok": True, "tokens_per_second": 4.9}, gates
    )
    assert not bad


def test_compare_metrics_no_tps_gate_unchanged():
    gates = {"require_generation": True}
    _, ok = compare_metrics(
        {}, {"generation_ok": True, "tokens_per_second": 0.0}, gates
    )
    assert ok
