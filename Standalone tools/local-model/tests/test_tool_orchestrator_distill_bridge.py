# -*- coding: utf-8 -*-
"""GovernedToolExecutor 工具橋 + 蒸餾快照→受管資料集橋。"""
from __future__ import annotations

import hashlib
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

import _xingcheng_test_support  # noqa: F401,E402

from native_transformer.inference.chat_session import SessionReply  # noqa: E402
from xingcheng.application.native_tool_orchestrator import (  # noqa: E402
    GovernedToolExecutor,
    TOOL_COMMAND_MAP,
)
from xingcheng.infrastructure.distill_dataset_bridge import (  # noqa: E402
    queue_distillation_sft_job,
    register_distillation_snapshot,
)
from xingcheng.infrastructure.transformer_training_repository import (  # noqa: E402
    TransformerTrainingRepository,
)


# ── stubs ─────────────────────────────────────────────────────
class _StubService:
    """async handle(command, payload) → (event, result)。"""

    def __init__(self, *, boom: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.boom = boom

    async def handle(self, command, payload):
        self.calls.append((command, dict(payload)))
        if self.boom:
            raise RuntimeError("backend down")
        return f"{command}_result", {
            "ok": True,
            "echo": payload.get("query") or payload.get("question") or "",
        }


class _StubSession:
    """假的 ChatSession：依序回傳預設 SessionReply。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.tool_results: list[tuple[str, str]] = []
        self.user_inputs: list[str] = []

    def step(self, user_content=None, **_kwargs):
        if user_content is not None:
            self.user_inputs.append(user_content)
        return self.replies.pop(0)

    def add_tool_result(self, content, *, name=None):
        self.tool_results.append((name, content))


def _reply(text, calls=()):
    return SessionReply(
        text=text, raw_text=text, tool_calls=tuple(calls),
        generated_tokens=1, stopped_by_eos=True,
    )


# ── GovernedToolExecutor ──────────────────────────────────────
def test_tool_map_only_read_only_commands():
    """白名單不得含任何寫入型命令。"""
    commands = {entry["command"] for entry in TOOL_COMMAND_MAP.values()}
    forbidden = {
        "xingcheng_sql_save_knowledge", "xingcheng_sql_save_personality",
        "xingcheng_memory_review", "xingcheng_rag_ingest",
        "xingcheng_mobile_submit_investment_instruction",
    }
    assert commands.isdisjoint(forbidden)
    assert all(c.startswith("xingcheng_") for c in commands)


@pytest.mark.asyncio
async def test_execute_dispatches_and_filters_args():
    service = _StubService()
    executor = GovernedToolExecutor(service)
    outcome = await executor.execute(
        {"name": "rag_query", "arguments": {"question": "什麼是 RAG", "evil": "x", "top_k": "3"}}
    )
    assert outcome["ok"] is True
    command, payload = service.calls[0]
    assert command == "xingcheng_rag_query"
    assert payload["question"] == "什麼是 RAG"
    assert payload["top_k"] == 3
    assert payload["generate"] is False  # static 注入，模型不可覆寫
    assert "evil" not in payload  # 未白名單參數被丟棄


@pytest.mark.asyncio
async def test_execute_rejects_unknown_and_invalid():
    executor = GovernedToolExecutor(_StubService())
    denied = await executor.execute({"name": "shell_exec", "arguments": {}})
    assert denied["ok"] is False and denied["error_code"] == "TOOL_NOT_ALLOWED"
    invalid = await executor.execute({"name": "BAD NAME!!", "arguments": {}})
    assert invalid["error_code"] == "TOOL_CALL_INVALID"


@pytest.mark.asyncio
async def test_execute_tool_failure_does_not_raise():
    executor = GovernedToolExecutor(_StubService(boom=True))
    outcome = await executor.execute({"name": "git_status", "arguments": {}})
    assert outcome["ok"] is False
    assert outcome["error_code"] == "TOOL_EXECUTION_FAILED"
    assert "backend down" in outcome["result_text"]


@pytest.mark.asyncio
async def test_converse_runs_tool_round_then_final_text():
    service = _StubService()
    session = _StubSession(
        [
            _reply("", [{"name": "platform_status", "arguments": {}}]),
            _reply("平台狀態良好"),
        ]
    )
    executor = GovernedToolExecutor(service)
    result = await executor.converse(session, "平台現在怎麼樣")
    assert result["ok"] is True and result["text"] == "平台狀態良好"
    assert result["tool_rounds"] == 1 and result["rounds_exhausted"] is False
    assert service.calls[0][0] == "xingcheng_platform_status"
    name, content = session.tool_results[0]
    assert name == "platform_status" and '"ok": true' in content
    assert result["tool_trace"][0]["ok"] is True


@pytest.mark.asyncio
async def test_converse_caps_rounds():
    service = _StubService()
    session = _StubSession(
        [_reply("", [{"name": "git_status", "arguments": {}}]) for _ in range(10)]
    )
    executor = GovernedToolExecutor(service, max_rounds=2)
    result = await executor.converse(session, "hi")
    assert result["tool_rounds"] == 2
    assert result["rounds_exhausted"] is True
    assert len(service.calls) == 2  # 每輪一個呼叫


def test_available_tool_specs_validate():
    specs = GovernedToolExecutor.available_tool_specs()
    names = {spec["name"] for spec in specs}
    assert names == set(TOOL_COMMAND_MAP)
    assert all(spec["description"] for spec in specs)


# ── 蒸餾快照 → 受管資料集橋 ───────────────────────────────────
def _snapshot(tmp_path: Path, records: list[dict]) -> dict:
    path = tmp_path / "xingcheng" / "runtime" / "distill" / "snapshot.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "format_version": "star-transformer-sft/v1",
        "snapshot_path": str(path),
        "snapshot_sha256": sha,
        "examples": len(records),
        "teacher_models": ["qwen3.8:test"],
    }


def _record(text: str, split: str, quality: float = 0.9) -> dict:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "source": "teacher-distillation:qwen3.8:test",
        "sha256": digest,
        "text": text,
        "prompt": "p",
        "completion": "c",
        "intent": "chat",
        "quality_score": quality,
        "split": split,
    }


def test_register_distillation_snapshot(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    records = [_record(f"t{i}", "train") for i in range(4)] + [
        _record("v1", "validation")
    ]
    dataset = register_distillation_snapshot(
        repository, _snapshot(tmp_path, records)
    )
    assert dataset["state"] == "prepared"
    assert dataset["example_count"] == 5
    assert dataset["source_type"] if "source_type" in dataset else True
    # 重複註冊 → 內容定址回傳既有列
    again = register_distillation_snapshot(
        repository, _snapshot(tmp_path, records)
    )
    assert again["dataset_id"] == dataset["dataset_id"]
    assert again["inserted"] is False


def test_register_filters_low_quality_and_drift(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    records = [_record("t", "train"), _record("v", "validation", quality=0.5)]
    manifest = _snapshot(tmp_path, records)
    # 唯一 validation 被品質剔除 → 無 val 例 → create_dataset 拒絕
    with pytest.raises(ValueError):
        register_distillation_snapshot(repository, manifest)
    # 快照漂移
    drifted = dict(manifest)
    drifted["snapshot_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="DRIFT"):
        register_distillation_snapshot(repository, drifted)


def test_queue_distillation_sft_job(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    records = [_record(f"t{i}", "train") for i in range(3)] + [
        _record("v", "validation")
    ]
    result = queue_distillation_sft_job(
        repository, _snapshot(tmp_path, records),
        configuration={"tokenizer_dir": "runtime/models/tokenizer", "max_steps": 8},
    )
    assert result["job"]["status"] == "queued"
    config = json.loads(result["job"]["configuration_json"])
    assert config["training_kind"] == "sft"
    assert config["max_steps"] == 8
