"""星澄原生評估套件：``star-native-eval-suite/v1``。

套件檔是 JSON spec（held-out 文本、品質閘門）；``evaluate_checkpoint``
對 checkpoint 產生可重現 metrics（perplexity、生成健檢、吞吐）；
``compare_metrics`` 依閘門產出 comparison + passed；``run_evaluation``
一鍵寫進 ``transformer_adapter_evaluation``——套件雜湊入冊，
同一 adapter 同一套件只能評一次。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .native_transformer.benchmark import compare_perplexity, run_benchmark

SUITE_FORMAT_VERSION = "star-native-eval-suite/v1"

_REQUIRED_KEYS = ("suite_id", "eval_text", "quality_gates")


def load_suite(path: str | Path) -> dict[str, Any]:
    """讀入並驗證評估套件 spec；回傳含 ``suite_sha256`` 的 dict。"""
    spec_path = Path(path).resolve()
    raw = spec_path.read_text(encoding="utf-8")
    try:
        suite = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"eval suite is not valid JSON: {exc}") from exc
    if not isinstance(suite, dict):
        raise ValueError("eval suite must be a JSON object")
    missing = [key for key in _REQUIRED_KEYS if key not in suite]
    if missing:
        raise ValueError(f"eval suite missing keys: {missing}")
    if str(suite.get("format_version") or SUITE_FORMAT_VERSION) != (
        SUITE_FORMAT_VERSION
    ):
        raise ValueError("eval suite format_version mismatch")
    canonical = json.dumps(suite, ensure_ascii=False, sort_keys=True)
    suite["suite_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return suite


def evaluate_checkpoint(
    checkpoint_path: str | Path,
    suite: Mapping[str, Any],
    *,
    quantize: int | None = None,
) -> dict[str, Any]:
    """對單一 checkpoint 跑套件：perplexity + 生成健檢 + 吞吐。"""
    prompt = str(suite.get("sanity_prompt") or "def main():")
    max_new = int(suite.get("sanity_max_new_tokens") or 16)
    report = run_benchmark(
        checkpoint_path,
        prompt=prompt,
        max_new_tokens=max_new,
        repeat=1,
        seed=int(suite.get("seed") or 42),
        quantize=quantize,
        eval_text=str(suite["eval_text"]),
    )
    generation_ok = bool(report["generated_tokens"] > 0)
    return {
        "perplexity": report.get("eval_perplexity"),
        "generation_ok": generation_ok,
        "tokens_per_second": report["tokens_per_second"],
        "latency_ms_mean": report["latency_ms_mean"],
        "quantization": report["quantization"],
        "checkpoint_sha256": report["checkpoint_sha256"],
    }


def compare_metrics(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    quality_gates: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """依品質閘門評定 candidate 是否勝過 baseline。回傳 (comparison, passed)。"""
    gates = dict(quality_gates)
    max_regression = float(gates.get("max_perplexity_regression_pct", 5.0))
    require_generation = bool(gates.get("require_generation", True))
    min_tps = float(gates.get("min_tokens_per_second") or 0.0)

    base_ppl = baseline.get("perplexity")
    cand_ppl = candidate.get("perplexity")
    ppl_delta_pct = None
    ppl_ok = True
    if base_ppl is not None and cand_ppl is not None and float(base_ppl) > 0:
        ppl_delta_pct = (float(cand_ppl) - float(base_ppl)) / float(base_ppl) * 100.0
        ppl_ok = ppl_delta_pct <= max_regression

    generation_ok = (
        bool(candidate.get("generation_ok")) if require_generation else True
    )
    tps_ok = (
        float(candidate.get("tokens_per_second") or 0.0) >= min_tps
        if min_tps > 0
        else True
    )
    passed = ppl_ok and generation_ok and tps_ok
    comparison = {
        "perplexity_delta_pct": (
            round(ppl_delta_pct, 4) if ppl_delta_pct is not None else None
        ),
        "perplexity_gate": f"<= {max_regression}%",
        "perplexity_ok": ppl_ok,
        "generation_ok": generation_ok,
        "tokens_per_second_ok": tps_ok,
    }
    return comparison, passed


def _run_capability_evaluation(
    repository: Any,
    *,
    adapter_id: str,
    candidate_checkpoint: str | Path,
    suite_path: str | Path,
    baseline_checkpoint: str | Path | None,
    evaluated_by: str,
) -> dict[str, Any]:
    """``star-capability-suite/v1`` 九類評估路徑（Phase 5G 回歸閘門接線）。

    candidate 與 baseline 各自跑九類評估，以 ``compare_reports``
    的逐類別 pass_rate 回歸判定；任一類別下降即 fail-closed。
    """
    from .native_transformer.capability_eval import (
        compare_reports,
        evaluate_checkpoint as capability_evaluate,
        load_suite as load_capability_suite,
    )

    suite = load_capability_suite(suite_path)
    candidate_report = capability_evaluate(candidate_checkpoint, suite)
    baseline_report = (
        capability_evaluate(baseline_checkpoint, suite)
        if baseline_checkpoint
        else {"categories": {}}
    )
    comparison = compare_reports(baseline_report, candidate_report)
    passed = bool(comparison["passed"])
    record = repository.record_adapter_evaluation(
        adapter_id=str(adapter_id),
        suite_id=str(suite["suite_id"]),
        baseline_metrics={"categories": baseline_report.get("categories")},
        adapter_metrics={
            "categories": candidate_report.get("categories"),
            "overlap_free": candidate_report.get("overlap", {}).get(
                "overlap_free", True
            ),
        },
        comparison=comparison,
        quality_gates={"rule": "no category pass_rate regression"},
        passed=passed,
        evaluated_by=evaluated_by,
        suite_sha256=str(suite["suite_sha256"]),
    )
    return {
        "ok": True,
        "evaluation": record,
        "suite_sha256": suite["suite_sha256"],
        "comparison": comparison,
        "passed": passed,
    }


def run_evaluation(
    repository: Any,
    *,
    adapter_id: str,
    candidate_checkpoint: str | Path,
    suite_path: str | Path,
    baseline_checkpoint: str | Path | None = None,
    evaluated_by: str = "star-main-native-model",
) -> dict[str, Any]:
    """完整評估流程：載入套件 → 評 candidate（與 baseline）→ 閘門判定
    → 寫入 transformer_adapter_evaluation。

    依套件 ``format_version`` 分派：``star-capability-suite/v1`` 走九類
    能力評估＋逐類別回歸閘門；其餘走 perplexity 閘門路徑。
    """
    head = json.loads(Path(suite_path).read_text(encoding="utf-8"))
    if head.get("format_version") == "star-capability-suite/v1":
        return _run_capability_evaluation(
            repository,
            adapter_id=adapter_id,
            candidate_checkpoint=candidate_checkpoint,
            suite_path=suite_path,
            baseline_checkpoint=baseline_checkpoint,
            evaluated_by=evaluated_by,
        )
    suite = load_suite(suite_path)
    candidate_metrics = evaluate_checkpoint(candidate_checkpoint, suite)
    if baseline_checkpoint:
        baseline_metrics = evaluate_checkpoint(baseline_checkpoint, suite)
    else:
        # 無外部基線時以套件宣告的 baseline 常數對比
        baseline_metrics = dict(suite.get("baseline_metrics") or {})
    comparison, passed = compare_metrics(
        baseline_metrics, candidate_metrics, dict(suite["quality_gates"])
    )
    record = repository.record_adapter_evaluation(
        adapter_id=str(adapter_id),
        suite_id=str(suite["suite_id"]),
        baseline_metrics=baseline_metrics,
        adapter_metrics=candidate_metrics,
        comparison=comparison,
        quality_gates=dict(suite["quality_gates"]),
        passed=passed,
        evaluated_by=evaluated_by,
        suite_sha256=str(suite["suite_sha256"]),
    )
    return {
        "ok": True,
        "evaluation": record,
        "suite_sha256": suite["suite_sha256"],
        "comparison": comparison,
        "passed": passed,
    }


__all__ = [
    "SUITE_FORMAT_VERSION",
    "compare_metrics",
    "compare_perplexity",
    "evaluate_checkpoint",
    "load_suite",
    "run_evaluation",
]
