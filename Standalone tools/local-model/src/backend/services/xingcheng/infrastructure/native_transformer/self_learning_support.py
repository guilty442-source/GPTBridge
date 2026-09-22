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
MODEL_ID = "xingcheng-native"


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
    total = sum(len(records) for records in examples.values())
    trained_total = int(state.get("trained_example_total") or 0)
    new_examples = max(0, total - trained_total)
    if not examples:
        return {"ok": True, "action": "idle", "reason": "no-verified-examples", "total_examples": 0}
    if not force and new_examples < int(resolved_policy.min_new_examples):
        return {
            "ok": True,
            "action": "idle",
            "reason": "below-threshold",
            "total_examples": total,
            "new_examples": new_examples,
            "threshold": int(resolved_policy.min_new_examples),
        }

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

    summary = {
        "ok": True,
        "action": action,
        "job_id": job_id,
        "adapter_id": adapter_id,
        "dataset_id": str(dataset["dataset_id"]),
        "new_examples": new_examples,
        "total_examples": total,
        "evaluations": evaluations,
        "released": released,
        "runtime_checkpoint": pinned,
        "previous_weights_pruned": pruned,
        "checked_at": _iso_now(),
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
            "active_weights_version": lifecycle.active_weights_version,
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
