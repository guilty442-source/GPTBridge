"""Tests for the repair learning engine and self-upgrading repair service."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "main-system" / "src-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from tasks.repair_learning import (  # noqa: E402
    ErrorSignature,
    LEARN_PROMOTION_THRESHOLD,
    LearnedRecipe,
    RepairLearner,
    RepairLearningStore,
    RepairOutcome,
    _normalize_error_signature,
)
from tasks.central_repair import CentralRepairService  # noqa: E402


# ---------------------------------------------------------------------------
# ErrorSignature normalization
# ---------------------------------------------------------------------------


def test_error_signature_is_stable_across_variable_parts() -> None:
    sig_a = _normalize_error_signature(
        "IndentationError", "unexpected indent at line 42 in E:\\foo\\bar.py"
    )
    sig_b = _normalize_error_signature(
        "IndentationError", "unexpected indent at line 99 in E:\\baz\\qux.py"
    )
    assert sig_a == sig_b  # line numbers and paths are normalized away


def test_error_signature_differs_by_error_class() -> None:
    sig_a = _normalize_error_signature("SyntaxError", "bad syntax")
    sig_b = _normalize_error_signature("IndentationError", "bad syntax")
    assert sig_a != sig_b


# ---------------------------------------------------------------------------
# RepairLearningStore
# ---------------------------------------------------------------------------


def test_learning_store_records_and_retrieves_errors(tmp_path: Path) -> None:
    store = RepairLearningStore(tmp_path)
    sig = ErrorSignature(
        signature_hash="abc123",
        error_class="SyntaxError",
        message_pattern="bad syntax",
        failure_code="MAIN_SYSTEM_SOURCE_SYNTAX_FAILED",
    )
    store.record_error(sig)
    store.record_error(sig)  # second occurrence
    sigs = store.get_all_error_signatures()
    assert len(sigs) == 1
    assert sigs[0]["occurrence_count"] == 2


def test_learning_store_records_outcomes(tmp_path: Path) -> None:
    store = RepairLearningStore(tmp_path)
    sig = ErrorSignature(
        signature_hash="def456",
        error_class="IndentationError",
        message_pattern="unexpected indent",
        failure_code="MAIN_SYSTEM_SOURCE_SYNTAX_FAILED",
    )
    store.record_error(sig)
    outcome = RepairOutcome(
        run_id="run-1",
        signature_hash=sig.signature_hash,
        remedy="indentation-repair",
        ok=True,
    )
    store.record_outcome(outcome)
    outcomes = store.get_outcomes_for_signature(sig.signature_hash)
    assert len(outcomes) == 1
    assert outcomes[0]["ok"] is True
    assert outcomes[0]["remedy"] == "indentation-repair"


def test_learning_store_saves_and_retrieves_learned_recipes(tmp_path: Path) -> None:
    store = RepairLearningStore(tmp_path)
    recipe = LearnedRecipe(
        recipe_id="learned-test-1",
        name="Test learned recipe",
        failure_signatures=("SyntaxError", "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED"),
        remedy="indentation-repair",
        owner="main-system",
        occurrence_count=3,
        success_rate=1.0,
    )
    store.save_learned_recipe(recipe)
    recipes = store.get_learned_recipes()
    assert len(recipes) == 1
    assert recipes[0]["recipe_id"] == "learned-test-1"
    assert recipes[0]["success_rate"] == 1.0


# ---------------------------------------------------------------------------
# RepairLearner pattern promotion
# ---------------------------------------------------------------------------


def test_learner_promotes_pattern_after_threshold(tmp_path: Path) -> None:
    store = RepairLearningStore(tmp_path)
    learner = RepairLearner(store)
    sig = ErrorSignature(
        signature_hash=_normalize_error_signature(
            "IndentationError", "unexpected indent", file_path="main.py"
        ),
        error_class="IndentationError",
        message_pattern="unexpected indent",
        failure_code="MAIN_SYSTEM_SOURCE_SYNTAX_FAILED",
        file_context="main.py",
    )
    # Record enough successful outcomes to trigger promotion.
    for i in range(LEARN_PROMOTION_THRESHOLD):
        outcome = RepairOutcome(
            run_id=f"run-{i}",
            signature_hash=sig.signature_hash,
            remedy="indentation-repair",
            ok=True,
        )
        result = learner.learn_from_outcome(sig, outcome)
    assert result["promoted"] is True
    recipe = result["recipe"]
    assert recipe["remedy"] == "indentation-repair"
    assert recipe["success_rate"] == 1.0
    assert recipe["source"] == "learned"


def test_learner_does_not_promote_with_single_occurrence(tmp_path: Path) -> None:
    store = RepairLearningStore(tmp_path)
    learner = RepairLearner(store)
    sig = ErrorSignature(
        signature_hash="single-occurrence",
        error_class="SyntaxError",
        message_pattern="rare error",
        failure_code="UNKNOWN",
    )
    outcome = RepairOutcome(
        run_id="run-1",
        signature_hash=sig.signature_hash,
        remedy="manual-fix",
        ok=True,
    )
    result = learner.learn_from_outcome(sig, outcome)
    assert result["promoted"] is False


def test_learner_suggests_best_remedy(tmp_path: Path) -> None:
    store = RepairLearningStore(tmp_path)
    learner = RepairLearner(store)
    sig = ErrorSignature(
        signature_hash="suggest-test",
        error_class="ImportError",
        message_pattern="no module named",
        failure_code="PROCESS_START_FAILED",
    )
    # Record a failed attempt and a successful one.
    learner.learn_from_outcome(
        sig,
        RepairOutcome(run_id="r1", signature_hash="suggest-test", remedy="restart", ok=False),
    )
    learner.learn_from_outcome(
        sig,
        RepairOutcome(run_id="r2", signature_hash="suggest-test", remedy="reinstall", ok=True),
    )
    suggestion = learner.suggest_remedy(sig)
    assert suggestion["suggested"] is True
    assert suggestion["remedy"] == "reinstall"
    assert suggestion["success_rate"] == 1.0


def test_learner_analyze_history(tmp_path: Path) -> None:
    store = RepairLearningStore(tmp_path)
    learner = RepairLearner(store)
    sig = ErrorSignature(
        signature_hash="analyze-test",
        error_class="RuntimeError",
        message_pattern="crash",
        failure_code="BACKEND_CONNECTION_FAILED",
    )
    for i in range(3):
        learner.learn_from_outcome(
            sig,
            RepairOutcome(run_id=f"r{i}", signature_hash="analyze-test", remedy="restart", ok=True),
        )
    analysis = learner.analyze_history()
    assert analysis["total_error_types"] == 1
    assert analysis["total_error_occurrences"] == 3
    assert analysis["recurring_errors"] == 1
    assert analysis["learned_recipes"] >= 1


# ---------------------------------------------------------------------------
# CentralRepairService learning integration
# ---------------------------------------------------------------------------


def test_central_repair_status_includes_learning(tmp_path: Path) -> None:
    repair_root = tmp_path / "repair"
    repair_root.mkdir()
    svc = CentralRepairService(tmp_path, repair_root)
    status = svc.status()
    assert status["self_upgrading"] is True
    assert status["learning_enabled"] is True
    assert "learned_recipe_count" in status["knowledge_base"]


def test_central_repair_known_recipes_includes_learned(tmp_path: Path) -> None:
    repair_root = tmp_path / "repair"
    repair_root.mkdir()
    svc = CentralRepairService(tmp_path, repair_root)
    # Manually inject a learned recipe.
    recipe = LearnedRecipe(
        recipe_id="learned-injection-test",
        name="Injected learned recipe",
        failure_signatures=("ImportError",),
        remedy="reinstall",
        owner="main-system",
        occurrence_count=5,
        success_rate=0.8,
    )
    svc.learning_store.save_learned_recipe(recipe)
    recipes = svc.known_recipes()
    ids = [r.get("recipe_id") for r in recipes]
    assert "learned-injection-test" in ids


def test_central_repair_suggest_remedy_returns_empty_for_unknown(tmp_path: Path) -> None:
    repair_root = tmp_path / "repair"
    repair_root.mkdir()
    svc = CentralRepairService(tmp_path, repair_root)
    result = svc.suggest_remedy_for_error("NeverSeenError", "no history")
    assert result["suggested"] is False


def test_central_repair_learning_report(tmp_path: Path) -> None:
    repair_root = tmp_path / "repair"
    repair_root.mkdir()
    svc = CentralRepairService(tmp_path, repair_root)
    report = svc.learning_report()
    assert "total_error_types" in report
    assert "learned_recipes" in report
