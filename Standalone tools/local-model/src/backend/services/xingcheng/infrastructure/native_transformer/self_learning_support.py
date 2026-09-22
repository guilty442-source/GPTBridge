"""自我學習循環實作：訓練 → 評測 → 自動升級（與政策／狀態分離）。"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure all required modules are importable (shared_layer -> governance_rule -> main-system)
# The tool_root is the local-model directory; shared-layer is in the main GPTBridge repo
_TOOL_ROOT = Path(__file__).resolve().parents[6]
_PROJECT_ROOT = _TOOL_ROOT.parents[1]  # E:\GPTBridge
for _p in (
    str(_PROJECT_ROOT / "shared-layer" / "src"),
    str(_PROJECT_ROOT),
    str(_PROJECT_ROOT / "main-system" / "src-core"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .self_learning import (
    LOG_RELATIVE,
    SNAPSHOT_RELATIVE,
    SelfLearningPolicy,
    _iso_now,
    training_window_status,
    collect_verified_examples,
    load_policy,
    load_state,
    save_policy,
    save_state,
)

LIFECYCLE_RELATIVE = "xingcheng/runtime/models/lifecycle/xingcheng-native"
SETTINGS_RELATIVE = "runtime/settings/native-engine.json"
MATURITY_STATE_RELATIVE = "xingcheng/runtime/state/model-maturity.json"
MODEL_ID = "xingcheng-native"

# §2.7-3 課程映射：依 maturity 首個未達級決定本循環課程（每循環單一課程）
_CURRICULUM_COURSES: dict[int, str] = {
    5: "sft-dialogue",
    6: "sft-reasoning",
    7: "sft-evolution",
}


def _write_report(tool: Path, payload: Mapping[str, Any]) -> Path:
    directory = tool / LOG_RELATIVE
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"self-learning-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def _pin_runtime_checkpoint(tool: Path, artifact: Path) -> str:
    """把執行期設定指向新權重（自動升級的生效點）。"""
    settings_path = tool / SETTINGS_RELATIVE
    settings: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            settings = {}
    settings["checkpoint"] = artifact.relative_to(tool).as_posix()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp = settings_path.with_name(settings_path.name + ".tmp")
    temp.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(settings_path)
    return settings["checkpoint"]


def _retire_previous_artifact(previous: Path, keep: Path) -> bool:
    """只保留最新代：升級成功後移除先前的權重檔（含側檔）。"""
    if previous == keep or not previous.is_file():
        return False
    try:
        previous.unlink()
        sidecar = previous.with_name(previous.name + ".sha256")
        if sidecar.is_file():
            sidecar.unlink()
        return True
    except OSError:
        return False


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "action": "blocked", "reason": reason, **extra}


def _failure_breaker_status(
    policy: SelfLearningPolicy, state: Mapping[str, Any]
) -> dict[str, Any] | None:
    """§2.7-8 連續失敗熔斷：達上限即停，非 force 可繞（fail-closed）。"""
    limit = int(policy.max_consecutive_failures)
    streak = int(state.get("consecutive_failures") or 0)
    if limit <= 0 or streak < limit:
        return None
    return _blocked(
        "failure-breaker",
        consecutive_failures=streak,
        max_consecutive_failures=limit,
    )


def _min_interval_status(
    policy: SelfLearningPolicy, state: Mapping[str, Any]
) -> dict[str, Any] | None:
    """§2.7-1 最低間隔：距上次循環活動不足 min_interval_s 即略過。"""
    gap = int(policy.min_interval_s)
    last_run = state.get("last_run_at")
    if gap <= 0 or not last_run:
        return None
    try:
        last_dt = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
    except (ValueError, TypeError):
        return _blocked("invalid-last-run-at", last_run_at=last_run)
    if elapsed < gap:
        return _blocked(
            "min-interval", elapsed_s=round(elapsed, 1), min_interval_s=gap
        )
    return None


def _daily_budget_status(
    policy: SelfLearningPolicy, state: Mapping[str, Any]
) -> dict[str, Any] | None:
    """§2.7-4/8 每日訓練嘗試次數預算（UTC 日計）。"""
    cap = int(policy.max_cycles_per_day)
    if cap <= 0:
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counter = state.get("cycles_today")
    if (
        isinstance(counter, Mapping)
        and counter.get("date") == today
        and int(counter.get("count") or 0) >= cap
    ):
        return _blocked(
            "daily-cycle-budget", cycles_today=dict(counter), max_per_day=cap
        )
    return None


def _pool_stats(
    policy: SelfLearningPolicy,
    examples: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """範例池觀測：平均品質與合成／自我生成比例。"""
    prefixes = tuple(str(p) for p in policy.synthetic_source_prefixes)
    total = 0
    synthetic = 0
    quality_sum = 0.0
    for records in examples.values():
        for record in records:
            total += 1
            quality_sum += float(record.get("quality_score") or 0.0)
            if str(record.get("source_type") or "").startswith(prefixes):
                synthetic += 1
    return {
        "total": total,
        "synthetic_count": synthetic,
        "synthetic_ratio": (synthetic / total) if total else 0.0,
        "pool_avg_quality": (quality_sum / total) if total else 0.0,
    }


def _sanitize_pool(
    policy: SelfLearningPolicy,
    examples: Mapping[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """§2.7-2 資料就緒閘：去重＋探針值汙染排除（皆 fail-closed 剔除）。

    去重：``(input_text, target_text)`` 全等對跨 scope 只留首見
    （scope 依 dict 序迭代＝收集器的檔名排序，決定性）。
    汙染排除：input/target 含 maturity 探針值（沿用 chat-foundation
    ``PROBE_VALUES``）的範例一律剔除，防止評測值洩入訓練集。"""
    probe_values: frozenset[str] = frozenset()
    if policy.exclude_probe_values:
        from ..chat_foundation_dataset import PROBE_VALUES

        probe_values = PROBE_VALUES
    seen: set[tuple[str, str]] = set()
    cleaned: dict[str, list[dict[str, Any]]] = {}
    deduped = 0
    contaminated = 0
    for scope, records in examples.items():
        kept: list[dict[str, Any]] = []
        for record in records:
            prompt = str(record.get("input_text") or "")
            completion = str(record.get("target_text") or "")
            if probe_values and any(
                value in prompt or value in completion
                for value in probe_values
            ):
                contaminated += 1
                continue
            key = (prompt, completion)
            if policy.dedup_enabled:
                if key in seen:
                    deduped += 1
                    continue
                seen.add(key)
            kept.append(record)
        if kept:
            cleaned[scope] = kept
    return cleaned, {"deduped": deduped, "contaminated_dropped": contaminated}


def _apply_dataset_cap(
    examples: Mapping[str, list[dict[str, Any]]], cap: int
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """§2.7-8 資料上限：進資料集總例數超 cap 時決定性截斷。

    展平序＝scope 字典序 → 各 scope 內 revision 升冪（收集器已排序），
    保留前 ``cap`` 筆；回傳 (截斷後池, 截掉數)。"""
    if cap <= 0:
        return dict(examples), 0
    flat: list[tuple[str, dict[str, Any]]] = []
    for scope in sorted(examples):
        for record in examples[scope]:
            flat.append((scope, record))
    if len(flat) <= cap:
        return dict(examples), 0
    kept = flat[:cap]
    out: dict[str, list[dict[str, Any]]] = {}
    for scope, record in kept:
        out.setdefault(scope, []).append(record)
    return out, len(flat) - cap


def _quality_drift_status(
    policy: SelfLearningPolicy, stats: Mapping[str, Any]
) -> dict[str, Any] | None:
    """§2.7-1 品質漂移護欄：池平均品質低於下限即停（fail-closed）。"""
    floor = float(policy.min_pool_avg_quality)
    if floor <= 0:
        return None
    if float(stats["pool_avg_quality"]) < floor:
        return _blocked(
            "quality-drift",
            pool_avg_quality=round(float(stats["pool_avg_quality"]), 4),
            min_pool_avg_quality=floor,
        )
    return None


def _synthetic_ratio_status(
    policy: SelfLearningPolicy, stats: Mapping[str, Any]
) -> dict[str, Any] | None:
    """§2.7-2 合成比例上限：自我生成資料超上限即停（防自我放大）。"""
    cap = float(policy.max_synthetic_ratio)
    if cap <= 0 or not stats["total"]:
        return None
    if float(stats["synthetic_ratio"]) > cap:
        return _blocked(
            "synthetic-ratio-exceeded",
            synthetic_count=int(stats["synthetic_count"]),
            total_examples=int(stats["total"]),
            synthetic_ratio=round(float(stats["synthetic_ratio"]), 4),
            max_synthetic_ratio=cap,
        )
    return None


def _select_curriculum(
    policy: SelfLearningPolicy, tool: Path
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """§2.7-3 課程選擇：依 maturity 首個未達級選課程。

    回傳 ``(course_info, blocked)``；policy 未啟用時回固定 ``sft``。
    啟用時 maturity 狀態不可讀或 certified_level < 4（對話基礎未達）
    → fail-closed blocked。"""
    if not policy.curriculum_enabled:
        return {"course": "sft", "curriculum_enabled": False}, None
    path = tool / MATURITY_STATE_RELATIVE
    try:
        maturity = json.loads(path.read_text(encoding="utf-8"))
        certified = int(maturity["certified_level"])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None, _blocked(
            "maturity-state-unavailable", maturity_state=str(path)
        )
    if certified < 4:
        return None, _blocked(
            "curriculum-foundation-unmet", certified_level=certified
        )
    if certified >= 7:
        course, target = "sft-refresh", None
    else:
        target = certified + 1
        course = _CURRICULUM_COURSES[target]
    return (
        {
            "course": course,
            "target_level": target,
            "certified_level": certified,
            "curriculum_enabled": True,
        },
        None,
    )


def _degradation_probe(
    policy: SelfLearningPolicy, tool: Path
) -> dict[str, Any] | None:
    """§2.7-1 能力退化探針：以政策套件 baseline_metrics 量測現役權重。

    未啟用回 ``None``；否則回傳至少含 ``ok``／``degraded`` 的 dict。
    量測失敗 fail-closed＝``degraded=False``（不觸發訓練），錯誤保留。"""
    if not policy.degradation_probe_enabled:
        return None
    suite_rel = (
        Path("xingcheng") / "eval" / f"{policy.degradation_probe_suite}.json"
    )
    suite_path = tool / suite_rel
    if not suite_path.is_file():
        return {
            "ok": False,
            "degraded": False,
            "suite": str(policy.degradation_probe_suite),
            "error": "suite-missing",
        }
    try:
        from ..native_eval_suite import (
            compare_metrics,
            evaluate_checkpoint,
            load_suite,
        )
        from .lifecycle import ModelLifecycle

        lifecycle = ModelLifecycle.load_or_create(
            tool / LIFECYCLE_RELATIVE, MODEL_ID
        )
        active = lifecycle.active_weights()
        if active is None or not Path(str(active["path"])).is_file():
            return {
                "ok": False,
                "degraded": False,
                "suite": str(policy.degradation_probe_suite),
                "error": "active-weights-missing",
            }
        suite = load_suite(suite_path)
        metrics = evaluate_checkpoint(Path(str(active["path"])), suite)
        comparison, passed = compare_metrics(
            dict(suite.get("baseline_metrics") or {}),
            metrics,
            dict(suite["quality_gates"]),
        )
        return {
            "ok": True,
            "degraded": not passed,
            "suite": str(policy.degradation_probe_suite),
            "metrics": metrics,
            "comparison": comparison,
        }
    except Exception as exc:  # noqa: BLE001 — 探針失敗不觸發、記錄不吞沒
        return {
            "ok": False,
            "degraded": False,
            "suite": str(policy.degradation_probe_suite),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _inference_active() -> bool | None:
    """True when a xingcheng inference engine is cached (idle or in-flight).

    Returns None when the state cannot be determined — callers treat that
    as fail-closed block (§2.7-4 與推論互斥)."""
    try:
        from .. import native_engine
    except Exception:  # noqa: BLE001 — fail-closed signal, not silent pass
        return None
    cache = getattr(native_engine, "_engine_cache", None)
    if cache is None:
        return None
    return bool(cache)


def run_cycle_impl(
    tool_root: str | Path,
    *,
    policy: SelfLearningPolicy | None = None,
    train_fn: Callable[..., dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    tool = Path(tool_root).resolve()
    resolved_policy = policy or load_policy(tool)
    state = load_state(tool)
    if not resolved_policy.enabled:
        return {
            "ok": True,
            "action": "disabled",
            "policy": resolved_policy.to_dict(),
            "checked_at": _iso_now(),
        }

    breaker = _failure_breaker_status(resolved_policy, state)
    if breaker is not None:
        return {
            **breaker,
            "policy": resolved_policy.to_dict(),
            "checked_at": _iso_now(),
        }

    window = training_window_status(resolved_policy)
    if not window["allowed"]:
        return {
            "ok": True,
            "action": "blocked",
            "reason": window["reason"],
            "training_window": window,
            "policy": resolved_policy.to_dict(),
            "checked_at": _iso_now(),
        }

    for gate in (
        _min_interval_status(resolved_policy, state),
        _daily_budget_status(resolved_policy, state),
    ):
        if gate is not None:
            return {
                **gate,
                "policy": resolved_policy.to_dict(),
                "checked_at": _iso_now(),
            }

    if resolved_policy.inference_exclusion:
        active = _inference_active()
        if active is not False:
            return {
                "ok": True,
                "action": "blocked",
                "reason": (
                    "inference-active"
                    if active
                    else "inference-state-unavailable"
                ),
                "policy": resolved_policy.to_dict(),
                "checked_at": _iso_now(),
            }

    examples = collect_verified_examples(tool)
    if not examples:
        return {"ok": True, "action": "idle", "reason": "no-verified-examples", "total_examples": 0}
    # §2.7-2 去重＋探針值汙染排除（先於統計，護欄量測乾淨池）
    examples, sanitize_stats = _sanitize_pool(resolved_policy, examples)
    if not examples:
        return {
            "ok": True,
            "action": "idle",
            "reason": "pool-empty-after-sanitize",
            **sanitize_stats,
            "policy": resolved_policy.to_dict(),
            "checked_at": _iso_now(),
        }
    # §2.7-1/2 範例池護欄（品質漂移／合成比例；資料池劣化先於門檻暴露）
    stats = _pool_stats(resolved_policy, examples)
    stats.update(sanitize_stats)
    for gate in (
        _quality_drift_status(resolved_policy, stats),
        _synthetic_ratio_status(resolved_policy, stats),
    ):
        if gate is not None:
            return {
                **gate,
                "policy": resolved_policy.to_dict(),
                "checked_at": _iso_now(),
            }
    total = int(stats["total"])
    trained_total = int(state.get("trained_example_total") or 0)
    new_examples = max(0, total - trained_total)
    degradation_trigger: dict[str, Any] | None = None
    if not force and new_examples < int(resolved_policy.min_new_examples):
        # §2.7-1 能力退化探針：資料未達門檻時量測現役權重是否退化；
        # 退化且仍有可訓新例 → 降門檻觸發（其餘閘門不變）
        probe = _degradation_probe(resolved_policy, tool)
        if probe is not None and probe.get("degraded"):
            if new_examples < int(resolved_policy.degradation_min_examples):
                return {
                    "ok": True,
                    "action": "idle",
                    "reason": "degradation-detected-insufficient-data",
                    "degradation_probe": probe,
                    "total_examples": total,
                    "new_examples": new_examples,
                    "threshold": int(resolved_policy.min_new_examples),
                    "policy": resolved_policy.to_dict(),
                    "checked_at": _iso_now(),
                }
            degradation_trigger = probe
        else:
            result = {
                "ok": True,
                "action": "idle",
                "reason": "below-threshold",
                "total_examples": total,
                "new_examples": new_examples,
                "threshold": int(resolved_policy.min_new_examples),
            }
            if probe is not None:
                result["degradation_probe"] = probe
            return result

    # §2.7-3 課程選擇：決定本循環課程（單一課程；失敗即停由熔斷閘門承擔）
    curriculum, curriculum_blocked = _select_curriculum(resolved_policy, tool)
    if curriculum_blocked is not None:
        return {
            **curriculum_blocked,
            "policy": resolved_policy.to_dict(),
            "checked_at": _iso_now(),
        }
    course_intents = resolved_policy.curriculum_intent_map.get(
        str(curriculum["course"])
    )
    if course_intents:
        wanted = {str(intent) for intent in course_intents}
        examples = {
            scope: [
                record
                for record in records
                if str(record.get("intent") or "") in wanted
            ]
            for scope, records in examples.items()
        }
        examples = {scope: recs for scope, recs in examples.items() if recs}
        if not examples:
            return {
                **_blocked(
                    "course-dataset-empty",
                    course=str(curriculum["course"]),
                    intents=sorted(wanted),
                ),
                "policy": resolved_policy.to_dict(),
                "checked_at": _iso_now(),
            }
        curriculum["intent_filter"] = sorted(wanted)
        total = sum(len(recs) for recs in examples.values())
        new_examples = max(0, total - trained_total)

    # §2.7-8 資料上限：進資料集總例數超政策上限時決定性截斷
    examples, dataset_cap_truncated = _apply_dataset_cap(
        examples, int(resolved_policy.max_dataset_examples)
    )
    dataset_examples = sum(len(recs) for recs in examples.values())

    from ..native_eval_suite import run_evaluation
    from ..sft_dataset import build_sft_dataset
    from ..training_job_executor import TrainingJobExecutor
    from ..transformer_training_repository import TransformerTrainingRepository
    from .lifecycle import ModelLifecycle

    snapshot_dir = tool / SNAPSHOT_RELATIVE
    snapshot_path = snapshot_dir / f"sft-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.jsonl"
    snapshot: dict[str, Any] | None = None
    last_error: Exception | None = None
    for permille in (
        int(resolved_policy.val_permille),
        200,
        350,
        500,
    ):
        try:
            snapshot = build_sft_dataset(
                output_path=snapshot_path,
                examples_by_scope=examples,
                val_permille=permille,
            )
            break
        except ValueError as error:
            last_error = error
    if snapshot is None:
        failure = {
            "ok": False,
            "action": "blocked",
            "reason": f"dataset-split-unavailable:{last_error}",
            "total_examples": total,
        }
        save_state(
            tool,
            {
                **state,
                "last_run_at": _iso_now(),
                "last_action": "training-failed",
                "last_error": failure["reason"],
                "consecutive_failures": int(
                    state.get("consecutive_failures") or 0
                )
                + 1,
            },
        )
        return failure

    repository = TransformerTrainingRepository(tool)
    dataset = repository.create_dataset(
        content_sha256=str(snapshot["content_sha256"]),
        snapshot_path=str(snapshot["snapshot_path"]),
        snapshot_sha256=str(snapshot["snapshot_sha256"]),
        examples=snapshot["examples"],
        source_manifest={
            **dict(snapshot["source_manifest"]),
            "origin": "self-learning",
        },
        created_by="star-self-learning",
    )

    lifecycle_dir = tool / LIFECYCLE_RELATIVE
    lifecycle = ModelLifecycle.load_or_create(lifecycle_dir, MODEL_ID)
    active = lifecycle.active_weights()
    if active is None or not Path(str(active["path"])).is_file():
        return {
            "ok": False,
            "action": "blocked",
            "reason": "active-weights-missing",
            "total_examples": total,
        }
    active_path = Path(str(active["path"]))

    repository_root = Path(repository.tool_root)
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={
            "training_kind": "sft",
            "tokenizer_dir": "runtime/tokenizers/xingcheng-bpe-8k-v1",
            "init_checkpoint": active_path.relative_to(repository_root).as_posix(),
            "preset": "base",
            "max_length": int(resolved_policy.max_length),
            "batch_size": int(resolved_policy.batch_size),
            "grad_accum": int(resolved_policy.grad_accum),
            "lr": float(resolved_policy.lr),
            "max_steps": int(resolved_policy.max_steps),
            "warmup_steps": int(resolved_policy.warmup_steps),
            "checkpoint_every": max(1, int(resolved_policy.max_steps) // 2),
            "eval_every": max(1, int(resolved_policy.max_steps) // 2),
            "log_every": max(1, int(resolved_policy.max_steps) // 8),
            "device": str(resolved_policy.device),
            "gpu_required_mb": int(resolved_policy.gpu_required_mb),
            "max_train_seconds": int(resolved_policy.train_time_budget_s),
            "max_train_vram_mb": int(resolved_policy.train_vram_budget_mb),
            "curriculum_course": str(curriculum["course"]),
            "maturity_target_level": curriculum.get("target_level"),
        },
        requested_by="star-self-learning",
    )
    job_id = str(job["job_id"])
    # §2.7-4/8：訓練嘗試在啟動前先計入每日預算（crash 也計入，fail-closed）
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counter = state.get("cycles_today")
    count = (
        int(counter.get("count") or 0)
        if isinstance(counter, Mapping) and counter.get("date") == today
        else 0
    )
    state = {**state, "cycles_today": {"date": today, "count": count + 1}}
    save_state(tool, {**state, "last_run_at": _iso_now()})
    executor = (
        TrainingJobExecutor(repository, train_fn=train_fn)
        if train_fn is not None
        else TrainingJobExecutor(repository)
    )
    report = executor.run_job(job_id)
    if report.get("ok") is not True:
        failure = {
            "ok": False,
            "action": "training-failed",
            "job_id": job_id,
            "error_code": report.get("error_code"),
            "error_message": report.get("error_message"),
            "total_examples": total,
        }
        save_state(
            tool,
            {
                **state,
                "last_run_at": _iso_now(),
                "last_action": "training-failed",
                "last_job_id": job_id,
                "last_error": failure.get("error_message"),
                "consecutive_failures": int(
                    state.get("consecutive_failures") or 0
                )
                + 1,
            },
        )
        failure["report"] = str(_write_report(tool, failure))
        return failure

    # §2.7-8 訓練中資源超支即停：executor 內嵌預算在步邊界中止訓練時，
    # 循環層 fail-closed——checkpoint 由 executor 註冊但不評估不啟用，
    # 計入連續失敗（熔斷語意與 training-failed 一致）。
    stopped_reason = (report.get("summary") or {}).get("stopped_reason")
    if stopped_reason:
        trainer_summary = report.get("summary") or {}
        failure = {
            "ok": False,
            "action": "resource-overspend",
            "job_id": job_id,
            "stopped_reason": stopped_reason,
            "total_examples": total,
            "resource_account": {
                "elapsed_seconds": trainer_summary.get("elapsed_seconds"),
                "steps": trainer_summary.get("steps"),
                "device": str(resolved_policy.device),
            },
        }
        save_state(
            tool,
            {
                **state,
                "last_run_at": _iso_now(),
                "last_action": "resource-overspend",
                "last_job_id": job_id,
                "last_error": stopped_reason,
                "consecutive_failures": int(
                    state.get("consecutive_failures") or 0
                )
                + 1,
            },
        )
        failure["report"] = str(_write_report(tool, failure))
        return failure

    artifact = Path(str(report["output_path"])).resolve()
    adapter = repository.register_adapter_candidate(
        job_id=job_id,
        artifact_path=artifact.relative_to(repository_root).as_posix(),
        metrics={
            "phase": "supervised-fine-tuning",
            "origin": "self-learning",
            "new_examples": new_examples,
            "total_examples": total,
        },
    )
    adapter_id = str(adapter["adapter_id"])

    evaluations: list[dict[str, Any]] = []
    all_passed = True
    for suite_name in resolved_policy.suites:
        suite_path = tool / "xingcheng" / "eval" / f"{suite_name}.json"
        if not suite_path.is_file():
            all_passed = False
            evaluations.append(
                {"suite": suite_name, "passed": False, "error": "suite-missing"}
            )
            continue
        result = run_evaluation(
            repository,
            adapter_id=adapter_id,
            candidate_checkpoint=artifact,
            suite_path=suite_path,
            baseline_checkpoint=active_path,
            evaluated_by="star-self-learning",
        )
        evaluations.append(
            {
                "suite": suite_name,
                "passed": bool(result["passed"]),
                "comparison": result.get("comparison"),
            }
        )
        all_passed = all_passed and bool(result["passed"])

    action = "rejected"
    released: list[str] = []
    pinned: str | None = None
    pruned = False
    if all_passed:
        staged = repository.release_adapter(
            adapter_id,
            "stage",
            governed_by="self-learning-policy",
            reason=f"auto: {new_examples} new verified examples",
        )
        released.append(str(staged["status"]))
        action = "staged"
        if resolved_policy.auto_activate:
            activated = repository.release_adapter(
                adapter_id,
                "activate",
                governed_by="self-learning-policy",
                reason="auto-activate after all evaluation gates passed",
            )
            released.append(str(activated["status"]))
            lifecycle.register_artifact(
                "weights",
                artifact,
                metadata={
                    "job_id": job_id,
                    "adapter_id": adapter_id,
                    "origin": "self-learning",
                    "new_examples": new_examples,
                },
                activate=True,
            )
            lifecycle.save(lifecycle_dir)
            pinned = _pin_runtime_checkpoint(tool, artifact)
            pruned = _retire_previous_artifact(active_path, artifact)
            action = "upgraded"

    # §2.7-9 資源帳：訓練耗時／步數／資料量／裝置，留於報告與狀態
    trainer_summary = report.get("summary") or {}
    resource_account = {
        "elapsed_seconds": trainer_summary.get("elapsed_seconds"),
        "steps": trainer_summary.get("steps"),
        "device": str(resolved_policy.device),
        "dataset_example_count": snapshot["manifest"].get("example_count"),
        "train_count": snapshot["manifest"].get("train_count"),
        "validation_count": snapshot["manifest"].get("validation_count"),
    }

    summary = {
        "ok": True,
        "action": action,
        "job_id": job_id,
        "adapter_id": adapter_id,
        "dataset_id": str(dataset["dataset_id"]),
        "curriculum": curriculum,
        "new_examples": new_examples,
        "total_examples": total,
        "dataset_examples": dataset_examples,
        "dataset_cap_truncated": dataset_cap_truncated,
        "trigger": (
            "degradation-probe" if degradation_trigger else "data-threshold"
        ),
        "degradation_probe": degradation_trigger,
        "evaluations": evaluations,
        "released": released,
        "runtime_checkpoint": pinned,
        "previous_weights_pruned": pruned,
        "resource_account": resource_account,
        "pool": stats,
        "checked_at": _iso_now(),
    }

    # §2.7-9 升級後 maturity 重測（政策啟用時；失敗記錄不中斷升級，
    # 因 rollback 語義待裁決——結果留審計供治理判定）
    if action == "upgraded" and resolved_policy.post_upgrade_maturity_recheck:
        try:
            from .maturity import certify, persist_report

            recheck_report = certify(
                checkpoint=artifact,
                tool_root=tool,
                device=str(resolved_policy.maturity_recheck_device),
            )
            recheck_path = persist_report(tool, recheck_report)
            summary["maturity_recheck"] = {
                "ok": True,
                "certified_level": recheck_report.get("certified_level"),
                "certified_level_name": recheck_report.get(
                    "certified_level_name"
                ),
                "report": str(recheck_path),
            }
        except Exception as exc:  # noqa: BLE001 — 記錄而非吞沒
            summary["maturity_recheck"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    save_state(
        tool,
        {
            **state,
            "trained_example_total": total,
            "last_run_at": _iso_now(),
            "last_action": action,
            "last_job_id": job_id,
            "last_adapter_id": adapter_id,
            "last_evaluations": evaluations,
            "last_course": curriculum.get("course"),
            "last_degradation_probe": summary.get("degradation_probe"),
            "last_maturity_recheck": summary.get("maturity_recheck"),
            "active_weights_version": lifecycle.active_weights_version,
            "resource_account": resource_account,
            "pool": stats,
            "consecutive_failures": 0,
            "last_error": None,
        },
    )
    summary["report"] = str(_write_report(tool, summary))
    return summary


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄自我學習與自動升級")
    parser.add_argument("--tool-root", default=None)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--force", action="store_true", help="忽略新範例門檻")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--watch", action="store_true", help="週期性執行循環")
    parser.add_argument("--interval", type=float, default=900.0, help="watch 間隔秒數")
    args = parser.parse_args(argv)

    from .self_learning import policy_path

    tool = (
        Path(args.tool_root).resolve()
        if args.tool_root
        else Path(__file__).resolve().parents[6]
    )
    policy = load_policy(tool)
    if args.enable or args.disable:
        policy.enabled = bool(args.enable)
        save_policy(tool, policy)
    if args.watch:
        interval = max(30.0, float(args.interval))
        print(
            json.dumps(
                {"event": "watch-start", "interval": interval, "tool_root": str(tool)},
                ensure_ascii=False,
            ),
            flush=True,
        )
        while True:
            cycle = run_cycle(tool, policy=load_policy(tool), force=False)
            print(json.dumps(cycle, ensure_ascii=False), flush=True)
            time.sleep(interval)
    if args.status or not args.run_once:
        print(
            json.dumps(
                {
                    "tool_root": str(tool),
                    "policy_path": str(policy_path(tool)),
                    "policy": policy.to_dict(),
                    "state": load_state(tool),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not args.run_once:
            return 0
    result = run_cycle(tool, policy=policy, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") is not False else 1


def run_cycle(
    tool_root: str | Path,
    *,
    policy: SelfLearningPolicy | None = None,
    train_fn: Callable[..., dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return run_cycle_impl(tool_root, policy=policy, train_fn=train_fn, force=force)


__all__ = ["run_cycle", "run_cycle_impl", "run_cli"]
