"""Repair teaching — governed learn.teach + model-bridge coverage.

The repair-teaching chain: the codex parent (星澄) issues bounded
``learn.teach`` commands; the learning sub-sovereign stores them as
``source="taught"`` recipes (doctrine, distinct from outcome-earned
``learned`` recipes); taught doctrine fills the cold-start gap in
``suggest_remedy`` and merges into ``known_recipes`` for ``plan_repair``;
accepted recipes are forwarded to the model's governed teaching gate
(``xingcheng_submit_teaching``) so repair knowledge settles into model
capability too.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tasks.repair_learning import RepairLearner, RepairLearningStore
from tasks.repair_learning_types import (
    TAUGHT_RECIPE_SOURCE,
    ErrorSignature,
    RepairOutcome,
    _iso_now,
)
from tasks.central_repair import CentralRepairService


def _learner(tmp_path: Path) -> RepairLearner:
    return RepairLearner(RepairLearningStore(tmp_path / "repair"))


def _signature(
    error_class: str = "TOOL_RUNTIME_CRASH", failure_code: str = ""
) -> ErrorSignature:
    return ErrorSignature(
        signature_hash="sig-hash-1",
        error_class=error_class,
        message_pattern="crash",
        failure_code=failure_code,
    )


# ---------------------------------------------------------------------------
# teach_recipe — bounded doctrine storage
# ---------------------------------------------------------------------------


def test_teach_recipe_stores_doctrine(tmp_path: Path) -> None:
    learner = _learner(tmp_path)
    result = learner.teach_recipe(
        name="工具崩潰→重建",
        failure_signatures=("TOOL_RUNTIME_CRASH", "TOOL_START_FAILED"),
        remedy="inspect-owned-databases,rebuild-tool-executable",
        verification="rebuilt executable starts",
    )
    assert result["taught"] is True
    recipe = result["recipe"]
    assert recipe["source"] == TAUGHT_RECIPE_SOURCE
    assert recipe["occurrence_count"] == 0
    assert recipe["success_rate"] == 0.0
    assert recipe["owner"] == "星澄"
    assert recipe["runtime_only"] is True
    stored = learner.store.get_learned_recipes()
    assert stored[0]["recipe_id"] == recipe["recipe_id"]
    assert stored[0]["verification"] == "rebuilt executable starts"


def test_teach_recipe_rejects_mutation_remedy(tmp_path: Path) -> None:
    learner = _learner(tmp_path)
    result = learner.teach_recipe(
        name="bad",
        failure_signatures=("X",),
        remedy="repair-main-system-source",
    )
    assert result["taught"] is False
    assert result["reason"] == "remedy-outside-teachable-vocabulary"
    assert learner.store.get_learned_recipes() == []


def test_teach_recipe_requires_signature(tmp_path: Path) -> None:
    learner = _learner(tmp_path)
    result = learner.teach_recipe(
        name="empty", failure_signatures=(), remedy="inspect-owned-databases"
    )
    assert result["taught"] is False
    assert result["reason"] == "failure-signatures-required"


def test_teach_recipe_is_idempotent(tmp_path: Path) -> None:
    learner = _learner(tmp_path)
    kwargs = {
        "name": "工具崩潰→重建",
        "failure_signatures": ("TOOL_RUNTIME_CRASH",),
        "remedy": "inspect-owned-databases,rebuild-tool-executable",
        "verification": "v1",
    }
    first = learner.teach_recipe(**kwargs)
    second = learner.teach_recipe(**{**kwargs, "verification": "v2"})
    assert first["recipe"]["recipe_id"] == second["recipe"]["recipe_id"]
    assert len(learner.store.get_learned_recipes()) == 1
    assert learner.store.get_learned_recipes()[0]["verification"] == "v2"


# ---------------------------------------------------------------------------
# suggest_remedy — taught fallback fills the cold-start gap
# ---------------------------------------------------------------------------


def test_suggest_remedy_falls_back_to_taught(tmp_path: Path) -> None:
    learner = _learner(tmp_path)
    learner.teach_recipe(
        name="工具崩潰→重建",
        failure_signatures=("TOOL_RUNTIME_CRASH",),
        remedy="inspect-owned-databases,rebuild-tool-executable",
        verification="starts",
    )
    suggestion = learner.suggest_remedy(_signature())
    assert suggestion["suggested"] is True
    assert suggestion["source"] == TAUGHT_RECIPE_SOURCE
    assert "rebuild-tool-executable" in suggestion["remedy"]


def test_suggest_remedy_prefers_outcome_history(tmp_path: Path) -> None:
    learner = _learner(tmp_path)
    learner.teach_recipe(
        name="工具崩潰→重建",
        failure_signatures=("TOOL_RUNTIME_CRASH",),
        remedy="rebuild-tool-executable",
        verification="starts",
    )
    outcome = RepairOutcome(
        run_id="r1",
        signature_hash="sig-hash-1",
        remedy="connection-watchdog",
        ok=True,
        detail={},
        recorded_at=_iso_now(),
    )
    learner.store.record_outcome(outcome)
    suggestion = learner.suggest_remedy(_signature())
    assert suggestion["remedy"] == "connection-watchdog"
    assert suggestion.get("source") != TAUGHT_RECIPE_SOURCE


# ---------------------------------------------------------------------------
# known_recipes merge — taught doctrine becomes plannable
# ---------------------------------------------------------------------------


def _central_repair(tmp_path: Path) -> CentralRepairService:
    return CentralRepairService(tmp_path, tmp_path / "automatic-repair")


def test_taught_recipe_merges_into_known_recipes(tmp_path: Path) -> None:
    service = _central_repair(tmp_path)
    service.learner.teach_recipe(
        name="工具崩潰→重建",
        failure_signatures=("TOOL_RUNTIME_CRASH",),
        remedy="inspect-owned-databases,rebuild-tool-executable",
        verification="rebuilt executable starts",
    )
    recipes = {r["recipe_id"]: r for r in service.known_recipes()}
    taught = next(
        r for r in recipes.values() if r.get("source") == "taught"
    )
    assert taught["verified_applicability"] is True
    assert taught["verification"] == "rebuilt executable starts"
    assert taught["actions"] == [
        "inspect-owned-databases",
        "rebuild-tool-executable",
    ]


def test_taught_recipe_plans_repair(tmp_path: Path) -> None:
    from tasks.repair_planning import plan_repair

    service = _central_repair(tmp_path)
    service.learner.teach_recipe(
        name="連線暫態→觀察不修",
        failure_signatures=("CONNECTION_DEGRADED",),
        remedy="no-action-required",
        verification="transient self-recovers",
    )
    plan = plan_repair("CONNECTION_DEGRADED", recipes=service.known_recipes())
    assert plan.blocked_reason == ""
    assert plan.source == "taught"
    assert plan.inspect_databases is True


def test_unverified_taught_recipe_does_not_plan(tmp_path: Path) -> None:
    from tasks.repair_planning import plan_repair

    service = _central_repair(tmp_path)
    service.learner.teach_recipe(
        name="unverified",
        failure_signatures=("SOME_NEW_FAULT",),
        remedy="inspect-owned-databases",
        verification="",
    )
    plan = plan_repair("SOME_NEW_FAULT", recipes=service.known_recipes())
    # The recipe matched but its declared verification is empty — the
    # plan is blocked on applicability, proving doctrine alone cannot
    # arm an automatic repair without a verification statement.
    assert plan.blocked_reason == "repair-applicability-not-verified"


# ---------------------------------------------------------------------------
# learn.teach adjudication on the sub-sovereign
# ---------------------------------------------------------------------------


def _entity(tmp_path: Path) -> Any:
    from governance.sovereigns.xingcheng_sovereign import XingchengSovereign

    app = SimpleNamespace(project_root=tmp_path)
    return XingchengSovereign(app)


def test_learn_teach_adjudication(tmp_path: Path) -> None:
    child = _entity(tmp_path)
    request = SimpleNamespace(
        payload={
            "signature": {
                "error_class": "TOOL_RUNTIME_CRASH",
                "failure_code": "TOOL_RUNTIME_CRASH",
            },
            "remedy": "inspect-owned-databases,rebuild-tool-executable",
            "name": "工具崩潰→重建",
            "verification": "rebuilt executable starts",
        }
    )
    outcome = child._adjudicate_learn_teach(request)
    assert outcome.accepted is True
    recipe = outcome.result["recipe"]
    assert recipe["source"] == "taught"
    assert child._learner is not None
    assert child._learner.store.get_learned_recipes()


def test_learn_teach_rejects_unbounded_remedy(tmp_path: Path) -> None:
    child = _entity(tmp_path)
    request = SimpleNamespace(
        payload={
            "signature": {"error_class": "X"},
            "remedy": "repair-main-system-source",
        }
    )
    outcome = child._adjudicate_learn_teach(request)
    assert outcome.accepted is False
    assert "remedy-outside-teachable-vocabulary" in str(
        outcome.refusal.reason_code
    )


def test_curriculum_applies_on_arm(tmp_path: Path) -> None:
    child = _entity(tmp_path)
    child._ensure_learner()
    result = child._apply_repair_curriculum()
    assert result["applied"] > 0
    again = child._apply_repair_curriculum()
    assert again["applied"] == result["applied"]
    recipes = child._learner.store.get_learned_recipes()
    assert all(r["source"] == "taught" for r in recipes)
    signatures = {
        token for r in recipes for token in r["failure_signatures"]
    }
    assert "TOOL_RUNTIME_CRASH" in signatures
    assert "CONNECTION_DEGRADED" in signatures


def test_bridge_emits_teaching_example(tmp_path: Path) -> None:
    submitted: list[tuple[str, str, dict[str, Any]]] = []
    permission = SimpleNamespace(
        submit_tool_execution_request=lambda tool, rid, payload: submitted.append(
            (tool, rid, payload)
        )
    )
    from governance.sovereigns.xingcheng_sovereign import XingchengSovereign

    app = SimpleNamespace(
        project_root=tmp_path, permission_sovereign=permission
    )
    child = XingchengSovereign(app)
    child._ensure_learner()
    child._adjudicate_learn_teach(
        SimpleNamespace(
            payload={
                "signature": {"error_class": "TOOL_RUNTIME_CRASH"},
                "remedy": "rebuild-tool-executable",
                "name": "工具崩潰→重建",
                "verification": "starts",
            }
        )
    )
    assert len(submitted) == 1
    tool, request_id, payload = submitted[0]
    assert tool == "xingcheng"
    assert payload["_governed_command"] == "xingcheng_submit_teaching"
    assert payload["training_intent"] == "repair"
    assert "TOOL_RUNTIME_CRASH" in payload["input_text"]
    assert payload["recipe_source"] == "taught"


def test_bridge_failure_does_not_fail_teach(tmp_path: Path) -> None:
    def _explode(tool: str, rid: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("channel down")

    permission = SimpleNamespace(submit_tool_execution_request=_explode)
    from governance.sovereigns.xingcheng_sovereign import XingchengSovereign

    app = SimpleNamespace(
        project_root=tmp_path, permission_sovereign=permission
    )
    child = XingchengSovereign(app)
    outcome = child._adjudicate_learn_teach(
        SimpleNamespace(
            payload={
                "signature": {"error_class": "X"},
                "remedy": "inspect-owned-databases",
            }
        )
    )
    assert outcome.accepted is True


def test_learn_teach_in_bounded_commands() -> None:
    from governance.sovereigns.xingcheng import learning_capability

    assert "learn.teach" in learning_capability._LEARNING_INTENTS


# ---------------------------------------------------------------------------
# Model side — repair intent in the teaching gate + command dispatch
# ---------------------------------------------------------------------------


def test_repair_intent_allowed() -> None:
    gate_path = (
        Path(__file__).resolve().parents[2]
        / "Standalone tools"
        / "local-model"
        / "src"
        / "backend"
        / "services"
        / "xingcheng"
        / "application"
        / "training_gate.py"
    )
    source = gate_path.read_text(encoding="utf-8")
    assert '"repair",' in source



def test_teaching_dispatch_registered() -> None:
    lifecycle_path = (
        Path(__file__).resolve().parents[2]
        / "Standalone tools"
        / "local-model"
        / "src"
        / "backend"
        / "services"
        / "xingcheng"
        / "application"
        / "local_ai_lifecycle.py"
    )
    lifecycle_source = lifecycle_path.read_text(encoding="utf-8")
    # dispatch 已重構為 registry 驅動：命令在 xingcheng_commands.py 註冊，
    # lifecycle 透過 resolve_command 分派（見 _create_teaching_commands）。
    assert "resolve_command" in lifecycle_source
    commands_path = (
        lifecycle_path.parent / "xingcheng_commands.py"
    )
    commands_source = commands_path.read_text(encoding="utf-8")
    assert '"xingcheng_submit_teaching"' in commands_source
    assert "_create_teaching_commands" in commands_source
