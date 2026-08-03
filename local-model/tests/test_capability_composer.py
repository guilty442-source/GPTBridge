from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from local_ai.domain.capability_composer import StarCapabilityComposer
from local_ai.domain.module_registry import StarModuleRegistry
from local_ai.application.service import LocalAiService


MODELS = {
    "coordinator_model": "qwen3.8:27b-q4_K_M",
    "coding_expert_model": "gpt-oss:20b",
    "mathematical_expert_model": "deepseek-r1:8b-0528-qwen3-q4_K_M",
    "release_reviewer_model": "qwen3.8:27b-q4_K_M",
    "training_coordinator_model": "deepseek-r1:8b-0528-qwen3-q4_K_M",
    "collaboration_coordinator_model": "gpt-oss:20b",
    "data_coordinator_model": "gemma4:e2b-it-qat",
}


def _request() -> dict[str, object]:
    return {
        "capability_name": "測試修復協調器",
        "capability_kind": "module",
        "objective": "分析程式問題、產生修正並執行測試。",
        "constraints": "不可修改治理規則，不可寫入資料庫。",
    }


def _votes(*decisions: str) -> list[dict[str, str]]:
    models = (
        MODELS["coordinator_model"],
        MODELS["training_coordinator_model"],
        MODELS["collaboration_coordinator_model"],
    )
    return [
        {"model": str(model), "vote": decision, "reason": f"{decision}-reason"}
        for model, decision in zip(models, decisions, strict=True)
    ]


def test_composition_uses_three_votes_and_three_mandatory_inspectors(
    tmp_path: Path,
) -> None:
    composer = StarCapabilityComposer(tmp_path / "local-ai", StarModuleRegistry())
    result = composer.compose(
        _request(), votes=_votes("approve", "reject", "approve"), **MODELS
    )

    discussion = result["model_discussion"]
    assert discussion["rule"] == "one-model-one-vote-simple-majority"
    assert discussion["approve_count"] == 2
    assert discussion["reject_count"] == 1
    assert discussion["majority_passed"] is True
    assert [item["model"] for item in discussion["inspection_gates"]] == [
        "deepseek-r1:8b-0528-qwen3-q4_K_M",
        "gpt-oss:20b",
        "qwen3.8:27b-q4_K_M",
    ]
    assert result["authority"]["governance_rule_mutable"] is False
    assert result["authority"]["database_write_performed"] is False
    assert result["authority"]["database_write_allowed_tools"] == [
        "ai-collaboration",
        "ai-assistant",
    ]


def test_source_write_is_denied_without_majority(tmp_path: Path) -> None:
    composer = StarCapabilityComposer(tmp_path / "local-ai", StarModuleRegistry())
    blueprint = composer.compose(
        _request(), votes=_votes("reject", "reject", "approve"), **MODELS
    )

    result = composer.apply(blueprint)

    assert result["ok"] is False
    assert result["error_code"] == "CAPABILITY_MAJORITY_VOTE_REQUIRED"
    assert result["authority"]["source_write_performed"] is False


def test_majority_approved_module_is_real_python_and_keeps_database_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    tool_root = tmp_path / "local-ai"
    composer = StarCapabilityComposer(tool_root, StarModuleRegistry())
    blueprint = composer.compose(
        _request(), votes=_votes("approve", "approve", "reject"), **MODELS
    )
    monkeypatch.setattr(
        "governance_rule.execution.audit.audit_runtime_governance", lambda _root: []
    )

    result = composer.apply(blueprint)

    target = tool_root / result["implementation_target"]
    source = target.read_text("utf-8")
    compile(source, str(target), "exec")
    assert result["ok"] is True
    assert result["status"] == "active-source-module"
    assert result["authority"]["source_write_performed"] is True
    assert result["database_write_performed"] is False
    assert "governance_rule" not in target.parts


class _GateRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, **kwargs):
        model = str(kwargs["requested_model"])
        self.calls.append(model)
        prompt = str(kwargs["prompt"])
        decision = "approve" if "approve|reject" in prompt else "pass"
        return {
            "ok": True,
            "text": f'{{"decision":"{decision}","reason":"測試通過"}}',
        }


@pytest.mark.asyncio
async def test_service_keeps_capability_composition_inside_star_native_model(
    tmp_path: Path, monkeypatch
) -> None:
    service = LocalAiService.__new__(LocalAiService)
    runtime = _GateRuntime()
    service.transformer_runtime = runtime
    service.capability_composer = StarCapabilityComposer(
        tmp_path / "local-ai", StarModuleRegistry()
    )
    monkeypatch.setattr(
        service.capability_composer,
        "apply",
        lambda blueprint: {
            **blueprint,
            "status": "active-source-module",
            "authority": {
                **blueprint["authority"],
                "source_write_performed": True,
            },
        },
    )
    request = {
        **_request(),
        "apply_changes": True,
        "owner_approved": True,
    }

    result = await service._compose_capability_with_vote(request)

    assert result["ok"] is True
    assert result["model_discussion"]["approve_count"] == 3
    assert result["model_discussion"]["all_inspections_passed"] is True
    assert result["composition_author_model"] == "star-main-native-model"
    assert result["model_assignments"]["composition_owner"] == (
        "star-native-internal-platform-validated"
    )
    assert result["external_ai_used"] is False
    assert result["ollama_models_used"] == []
    assert runtime.calls == []
    assert result["database_write_performed"] is False
