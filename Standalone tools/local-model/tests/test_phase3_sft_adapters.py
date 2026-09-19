"""Phase 3: star-transformer-sft/v1 serializer + adapter governance pipeline."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import hashlib
import json
from pathlib import Path

import pytest

from xingcheng.infrastructure.sft_dataset import (
    SFT_FORMAT_VERSION,
    build_sft_dataset,
    serialize_sft_example,
)
from xingcheng.infrastructure.training_job_executor import TrainingJobExecutor
from xingcheng.infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _example(example_id: str, text: str, quality: float = 0.9) -> dict:
    return {
        "revision": 1,
        "example_id": example_id,
        "intent": "conversation",
        "input_text": f"prompt-{text}",
        "target_text": f"completion-{text}",
        "source_type": "self-distillation-grounded",
        "quality_score": quality,
        "validation": {},
        "created_at": "2026-09-19T00:00:00Z",
    }


def _dataset(repo, tmp_path: Path, name: str = "ds") -> dict:
    built = build_sft_dataset(
        output_path=_tool(tmp_path) / f"runtime/state/training/{name}.jsonl",
        examples_by_scope={
            "main": [_example(f"{name}-a", "alpha"), _example(f"{name}-b", "beta")],
            "coding": [
                _example(f"{name}-c", "gamma"),
                _example(f"{name}-d", "delta"),
                _example(f"{name}-e", "epsilon"),
                _example(f"{name}-f", "zeta"),
                _example(f"{name}-g", "eta"),
                _example(f"{name}-h", "theta"),
                _example(f"{name}-i", "iota"),
                _example(f"{name}-j", "kappa"),
            ],
        },
        val_permille=200,
    )
    return repo.create_dataset(
        content_sha256=built["content_sha256"],
        snapshot_path=built["snapshot_path"],
        snapshot_sha256=built["snapshot_sha256"],
        examples=built["examples"],
        source_manifest=built["source_manifest"],
        format_version=SFT_FORMAT_VERSION,
    )


def _tool(tmp_path: Path) -> Path:
    return tmp_path / "xingcheng"


def _completed_job(repo, tmp_path: Path, name: str = "job") -> dict:
    dataset = _dataset(repo, tmp_path, name=name)
    job = repo.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"tokenizer_dir": "runtime/models/tokenizer"},
    )
    for status in ("preflight", "training", "validating", "completed"):
        repo.transition_training_job(str(job["job_id"]), status)
    return repo_jobs(repo, str(job["job_id"]))


def repo_jobs(repo, job_id: str) -> dict:
    with repo._connect() as connection:
        row = connection.execute(
            "SELECT * FROM transformer_training_job WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return dict(row)


def _artifact(tmp_path: Path, name: str = "adapter.bin") -> Path:
    path = _tool(tmp_path) / "runtime/models/adapters" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"adapter-weights-" + name.encode())
    return path


# ------------------------------------------------------------------ SFT


def test_sft_serializer_produces_deterministic_snapshot(tmp_path: Path) -> None:
    args = {
        "examples_by_scope": {
            "main": [_example(f"e{i}", f"x{i}") for i in range(20)],
        },
        "val_permille": 100,
    }
    first = build_sft_dataset(
        output_path=tmp_path / "a" / "snapshot.jsonl", **args
    )
    second = build_sft_dataset(
        output_path=tmp_path / "b" / "snapshot.jsonl", **args
    )

    assert first["format_version"] == SFT_FORMAT_VERSION
    assert first["content_sha256"] == second["content_sha256"]
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert len(first["examples"]) == 20
    assert first["manifest"]["train_count"] + first["manifest"]["validation_count"] == 20
    lines = Path(first["snapshot_path"]).read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["prompt"] and record["completion"] and record["text"]
    assert record["sha256"] == _sha(record["text"])


def test_sft_example_requires_nonempty_prompt_and_completion() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        serialize_sft_example({"input_text": "", "target_text": "x"})


def test_sft_dataset_registers_and_executor_splits_by_hash(
    tmp_path: Path,
) -> None:
    repo = TransformerTrainingRepository(tmp_path)
    dataset = _dataset(repo, tmp_path)
    assert dataset["format_version"] == SFT_FORMAT_VERSION
    assert dataset["example_count"] == 10

    executor = TrainingJobExecutor(repo)
    with repo._connect() as connection:
        splits = {
            str(r["content_sha256"]): str(r["split"])
            for r in connection.execute(
                "SELECT content_sha256, split "
                "FROM transformer_training_dataset_example WHERE dataset_id = ?",
                (dataset["dataset_id"],),
            ).fetchall()
        }
    train, val = executor._load_split_documents(dataset, splits)
    assert train and val
    assert all(d["sha256"] in splits for d in train + val)


def test_sft_dataset_rejects_unknown_scope(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="database_scope"):
        build_sft_dataset(
            output_path=tmp_path / "s.jsonl",
            examples_by_scope={"pirate": [_example("e1", "x")]},
        )


# ------------------------------------------------------------------ adapters


def test_adapter_candidate_requires_completed_job(tmp_path: Path) -> None:
    repo = TransformerTrainingRepository(tmp_path)
    dataset = _dataset(repo, tmp_path)
    job = repo.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"tokenizer_dir": "x"},
    )
    artifact = _artifact(tmp_path)

    with pytest.raises(ValueError, match="completed"):
        repo.register_adapter_candidate(
            job_id=str(job["job_id"]),
            artifact_path=str(artifact),
            metrics={},
        )


def test_adapter_artifact_must_stay_in_tool_root(tmp_path: Path) -> None:
    repo = TransformerTrainingRepository(tmp_path)
    job = _completed_job(repo, tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")

    with pytest.raises(PermissionError, match="ARTIFACT_SCOPE_DENIED"):
        repo.register_adapter_candidate(
            job_id=str(job["job_id"]),
            artifact_path=str(outside),
            metrics={},
        )


def test_adapter_full_governed_lifecycle(tmp_path: Path) -> None:
    repo = TransformerTrainingRepository(tmp_path)
    job = _completed_job(repo, tmp_path)
    artifact = _artifact(tmp_path)
    adapter = repo.register_adapter_candidate(
        job_id=str(job["job_id"]),
        artifact_path=str(artifact),
        metrics={"loss": 1.2},
        adapter_format="qlora-nf4-peft",
    )
    adapter_id = str(adapter["adapter_id"])
    assert adapter["status"] == "candidate"

    failed_eval = repo.record_adapter_evaluation(
        adapter_id=adapter_id,
        suite_id="star-eval-suite",
        baseline_metrics={"ppl": 100},
        adapter_metrics={"ppl": 120},
        comparison={"ppl_delta": 20},
        quality_gates={"max_ppl_regression": 10},
        passed=False,
    )
    assert failed_eval["passed"] == 0
    assert repo.adapter_candidate(adapter_id)["status"] == "candidate"

    repo.record_adapter_evaluation(
        adapter_id=adapter_id,
        suite_id="star-eval-suite-v2",
        baseline_metrics={"ppl": 100},
        adapter_metrics={"ppl": 90},
        comparison={"ppl_delta": -10},
        quality_gates={"max_ppl_regression": 10},
        passed=True,
    )
    assert repo.adapter_candidate(adapter_id)["status"] == "validated"

    with pytest.raises(ValueError, match="staged"):
        repo.release_adapter(
            adapter_id, "activate",
            governed_by="star-main-native-model", reason="premature",
        )

    repo.release_adapter(
        adapter_id, "stage",
        governed_by="star-main-native-model", reason="ready",
    )
    activated = repo.release_adapter(
        adapter_id, "activate",
        governed_by="star-main-native-model", reason="promote",
    )
    assert activated["status"] == "active"
    assert activated["runtime_state"]["active_adapter_id"] == adapter_id
    assert activated["runtime_state"]["automatic_weight_replacement"] == 0


def test_adapter_rollback_restores_previous_active(tmp_path: Path) -> None:
    repo = TransformerTrainingRepository(tmp_path)
    first = repo.register_adapter_candidate(
        job_id=str(_completed_job(repo, tmp_path, "j1")["job_id"]),
        artifact_path=str(_artifact(tmp_path, "a1.bin")),
        metrics={},
    )
    second = repo.register_adapter_candidate(
        job_id=str(_completed_job(repo, tmp_path, "j2")["job_id"]),
        artifact_path=str(_artifact(tmp_path, "a2.bin")),
        metrics={},
    )
    for adapter in (first, second):
        aid = str(adapter["adapter_id"])
        repo.record_adapter_evaluation(
            adapter_id=aid, suite_id="s", baseline_metrics={},
            adapter_metrics={}, comparison={}, quality_gates={},
            passed=True,
        )
        repo.release_adapter(aid, "stage", governed_by="gov", reason="ok")
        repo.release_adapter(aid, "activate", governed_by="gov", reason="go")

    state = repo._runtime_model_state()
    assert state["active_adapter_id"] == second["adapter_id"]
    assert state["previous_adapter_id"] == first["adapter_id"]
    assert repo.adapter_candidate(str(first["adapter_id"]))["status"] == "staged"

    rolled = repo.release_adapter(
        str(second["adapter_id"]), "rollback",
        governed_by="gov", reason="regression",
    )
    state = repo._runtime_model_state()
    assert state["active_adapter_id"] == first["adapter_id"]
    assert state["previous_adapter_id"] is None
    assert repo.adapter_candidate(str(first["adapter_id"]))["status"] == "active"
    assert rolled["status"] == "staged"


def test_adapter_reject_and_audit_chain(tmp_path: Path) -> None:
    repo = TransformerTrainingRepository(tmp_path)
    job = _completed_job(repo, tmp_path)
    adapter = repo.register_adapter_candidate(
        job_id=str(job["job_id"]),
        artifact_path=str(_artifact(tmp_path)),
        metrics={},
    )
    rejected = repo.reject_adapter(
        str(adapter["adapter_id"]),
        governed_by="gov", reason="below gate",
    )
    assert rejected["status"] == "rejected"
    assert repo.verify_audit_chain()["ok"] is True
    with pytest.raises(ValueError, match="cannot be rejected"):
        repo.reject_adapter(
            str(adapter["adapter_id"]), governed_by="gov", reason="again"
        )


# ------------------------------------------------- preference pairs + tool calls

from xingcheng.infrastructure.repository import LocalAiRepository
from xingcheng.infrastructure.tool_call_format import (
    TOOL_CALL_FORMAT_VERSION,
    decode_tool_calls,
    serialize_tool_call_example,
)


def _main_repo(tmp_path: Path) -> LocalAiRepository:
    return LocalAiRepository(tmp_path, "main")


def test_rejected_candidate_becomes_pending_preference_half(
    tmp_path: Path,
) -> None:
    repo = _main_repo(tmp_path)
    stored = repo.record_rejected_teaching_candidate(
        intent="reasoning",
        input_text="解釋遞迴",
        rejected_text="錯誤的回答內容",
        source_type="owner-governed-teaching-candidate",
        gate_verdict={"reasons": ["semantic-grounding-too-low"]},
    )

    assert stored["inserted"] is True
    assert stored["paired"] is False
    assert repo.language_preference_pairs() == []

    again = repo.record_rejected_teaching_candidate(
        intent="reasoning",
        input_text="解釋遞迴",
        rejected_text="錯誤的回答內容",
        source_type="owner-governed-teaching-candidate",
        gate_verdict={},
    )
    assert again["inserted"] is False


def test_accepted_teaching_completes_pending_preference_pair(
    tmp_path: Path,
) -> None:
    repo = _main_repo(tmp_path)
    repo.record_rejected_teaching_candidate(
        intent="reasoning",
        input_text="解釋遞迴",
        rejected_text="壞答案",
        source_type="owner-governed-teaching-candidate",
        gate_verdict={},
    )

    completed = repo.complete_preference_pairs(
        intent="reasoning",
        input_text="解釋遞迴",
        chosen_text="遞迴是函式呼叫自身的技巧。",
        chosen_example_id="star-train-abc",
    )
    assert completed == 1

    pairs = repo.language_preference_pairs()
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["chosen_text"] == "遞迴是函式呼叫自身的技巧。"
    assert pair["rejected_text"] == "壞答案"
    assert pair["chosen_example_id"] == "star-train-abc"
    assert repo.complete_preference_pairs(
        intent="reasoning",
        input_text="解釋遞迴",
        chosen_text="另一個答案",
        chosen_example_id="star-train-xyz",
    ) == 0


def test_preference_pair_requires_prompt_and_rejected_text(
    tmp_path: Path,
) -> None:
    repo = _main_repo(tmp_path)
    with pytest.raises(ValueError, match="prompt and rejected"):
        repo.record_rejected_teaching_candidate(
            intent="reasoning", input_text="", rejected_text="x",
            source_type="s", gate_verdict={},
        )


def test_tool_call_sample_roundtrip() -> None:
    record = serialize_tool_call_example(
        prompt="查一下台積電股價",
        tool_calls=[{"name": "stock_quote", "arguments": {"symbol": "2330.TW"}}],
        response_text="已查詢台積電最新報價。",
        tools=[{"name": "stock_quote", "description": "查股價"}],
    )

    assert record["format_version"] == TOOL_CALL_FORMAT_VERSION
    assert "<tool_call>" in record["completion"]
    calls = decode_tool_calls(record["completion"])
    assert calls == [
        {"name": "stock_quote", "arguments": {"symbol": "2330.TW"}}
    ]
    assert record["sha256"] == _sha(record["text"])


def test_tool_call_rejects_bad_name_and_oversized_args() -> None:
    with pytest.raises(ValueError, match="invalid tool name"):
        serialize_tool_call_example(
            prompt="p",
            tool_calls=[{"name": "BAD NAME!", "arguments": {}}],
        )
    with pytest.raises(ValueError, match="bounded size"):
        serialize_tool_call_example(
            prompt="p",
            tool_calls=[{"name": "ok_tool", "arguments": {"blob": "x" * 70_000}}],
        )
