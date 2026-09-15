"""Auto-repair chain regression tests.

Covers the optimized chain internals: real deterministic patching via
IndentationRepairer, rollback/finalize backup lifecycle, grant scope
boundary checks, batched dirty checks, honest rebuild outcomes, and
learning-store dedup/failure accounting.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from core_system.auto_repair_chain_executor import GovernedExecutor
from core_system.auto_repair_chain_learning import RepairLearningStore
from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    PermissionGrant,
    RepairPlan,
    VerificationResult,
)
from core_system import auto_repair_chain_util as util


# Orphan pattern: a statement that lost its indentation entirely, so it
# sits at column 0 between indented block members (IndentationError).
BROKEN_INDENT = "def f():\n    a = 1\nb = 2\n    return a\n"
VALID_SOURCE = "def f():\n    return 1\n"


def _executor(tmp_path: Path) -> GovernedExecutor:
    audit = GovernanceAudit(tmp_path / "audit")
    return GovernedExecutor(tmp_path, audit)


def _plan(tmp_path: Path, target: str, steps: list[dict]) -> RepairPlan:
    full = tmp_path / target
    import hashlib

    pre = {target: hashlib.sha256(full.read_bytes()).hexdigest()}
    return RepairPlan(
        plan_id="plan_test",
        objective_id="obj_test",
        method="targeted_patch",
        steps=steps,
        preimage_hashes=pre,
        verification_criteria=["compile-ok"],
        rollback_plan={},
        estimated_duration_seconds=60,
    )


def _grant(*paths: str) -> PermissionGrant:
    return PermissionGrant(
        grant_id="g1",
        target_entity="main-system",
        action="patch",
        path_scope=list(paths),
        data_scope=[],
        expires_at="",
    )


@pytest.fixture(autouse=True)
def clean_tree(monkeypatch: pytest.MonkeyPatch):
    """Tests run in tmp dirs outside git — pretend the tree is clean."""
    monkeypatch.setattr(util, "dirty_git_paths", lambda _root: set())


def test_targeted_patch_repairs_indentation(tmp_path: Path) -> None:
    target = "pkg/broken.py"
    full = tmp_path / target
    full.parent.mkdir(parents=True)
    full.write_text(BROKEN_INDENT, encoding="utf-8")

    executor = _executor(tmp_path)
    result = executor._apply_targeted_patch(target, None)

    assert result["ok"] is True
    assert result["changed"] is True
    ok, _ = util.file_compiles(full)
    assert ok
    # Rollback material is retained until finalize()
    assert (full.parent / "broken.py.repair_backup").exists()


def test_targeted_patch_rejects_supplied_patch(tmp_path: Path) -> None:
    target = "pkg/x.py"
    full = tmp_path / target
    full.parent.mkdir(parents=True)
    full.write_text(VALID_SOURCE, encoding="utf-8")

    executor = _executor(tmp_path)
    result = executor._apply_targeted_patch(target, "malicious overwrite")

    assert result["ok"] is False
    assert "whole-file-rewrite" in result["error"]
    assert full.read_text(encoding="utf-8") == VALID_SOURCE


def test_targeted_patch_noop_on_healthy_file(tmp_path: Path) -> None:
    target = "pkg/ok.py"
    full = tmp_path / target
    full.parent.mkdir(parents=True)
    full.write_text(VALID_SOURCE, encoding="utf-8")

    executor = _executor(tmp_path)
    result = executor._apply_targeted_patch(target, None)

    assert result["ok"] is True
    assert result["changed"] is False


def test_finalize_passed_discards_backup(tmp_path: Path) -> None:
    target = "pkg/broken.py"
    full = tmp_path / target
    full.parent.mkdir(parents=True)
    full.write_text(BROKEN_INDENT, encoding="utf-8")

    executor = _executor(tmp_path)
    plan = _plan(tmp_path, target, [{"action": "targeted_patch", "target": target}])
    executor._apply_targeted_patch(target, None)
    outcome = executor.finalize(plan, verification_passed=True)

    assert outcome["discarded"] == [target]
    assert not (full.parent / "broken.py.repair_backup").exists()


def test_finalize_failed_restores_preimage(tmp_path: Path) -> None:
    target = "pkg/broken.py"
    full = tmp_path / target
    full.parent.mkdir(parents=True)
    full.write_text(BROKEN_INDENT, encoding="utf-8")

    executor = _executor(tmp_path)
    plan = _plan(tmp_path, target, [{"action": "targeted_patch", "target": target}])
    executor._apply_targeted_patch(target, None)
    assert full.read_text(encoding="utf-8") != BROKEN_INDENT

    outcome = executor.finalize(plan, verification_passed=False)

    assert outcome["restored"] == [target]
    assert full.read_text(encoding="utf-8") == BROKEN_INDENT
    assert not (full.parent / "broken.py.repair_backup").exists()


def test_grant_scope_boundary(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    full = tmp_path / "src-core2" / "x.py"
    full.parent.mkdir(parents=True)
    full.write_text(VALID_SOURCE, encoding="utf-8")
    import hashlib

    plan = RepairPlan(
        plan_id="p", objective_id="o", method="targeted_patch",
        steps=[], preimage_hashes={"src-core2/x.py": hashlib.sha256(b"x").hexdigest()},
        verification_criteria=[], rollback_plan={}, estimated_duration_seconds=1,
    )
    # "src-core2/x.py" must NOT be covered by scope "src-core"
    assert executor._verify_grant(plan, _grant("src-core")) is False
    assert executor._verify_grant(plan, _grant("src-core2")) is True
    assert executor._verify_grant(plan, _grant("src-core2/x.py")) is True


def test_rebuild_artifact_fail_closed_on_bad_id(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor._rebuild_artifact("INVALID ID!", _grant("x"))
    assert result["ok"] is False
    assert result["error"] is not None


def test_config_reset_fail_closed(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor._reset_config("x", {})
    assert result["ok"] is False


def test_dirty_git_paths_none_means_unverifiable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(util, "dirty_git_paths", lambda _root: None)
    executor = _executor(tmp_path)
    plan = RepairPlan(
        plan_id="p", objective_id="o", method="targeted_patch",
        steps=[], preimage_hashes={"a.py": "h"},
        verification_criteria=[], rollback_plan={}, estimated_duration_seconds=1,
    )
    assert executor._has_uncommitted_changes(plan) is True


def test_iter_scope_files_bounded(tmp_path: Path) -> None:
    (tmp_path / "src" / "node_modules").mkdir(parents=True)
    (tmp_path / "src" / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "real.py").write_text("a=1", encoding="utf-8")
    (tmp_path / "src" / "big.bin").write_bytes(b"x" * (util.MAX_HASHED_FILE_BYTES + 1))

    found = {rel for rel, _ in util.iter_scope_files(tmp_path, "src")}
    assert "src/real.py" in found
    assert "src/node_modules/junk.js" not in found
    assert "src/big.bin" not in found


def test_learning_records_failures_and_dedups(tmp_path: Path) -> None:
    store = RepairLearningStore(tmp_path / "rep", GovernanceAudit(tmp_path / "a"))
    sig = "sig1"
    for ok, verdict in [(True, VerificationResult.PASSED)] * 3 + [
        (False, VerificationResult.FAILED)
    ]:
        store.record_outcome(
            signature_hash=sig, error_class="E", message_pattern="m",
            failure_code="E", remedy="targeted_patch", ok=ok,
            verification_result=verdict, detail={},
        )
    # 3/4 successes = 0.75 < 0.8 → not a candidate (failures now count)
    assert store.analyze_history(sig)["candidates"] == []

    store.record_outcome(
        signature_hash=sig, error_class="E", message_pattern="m",
        failure_code="E", remedy="targeted_patch", ok=True,
        verification_result=VerificationResult.PASSED, detail={},
    )
    candidates = store.analyze_history(sig)["candidates"]
    assert len(candidates) == 1  # 4/5 = 0.8, verified passed

    r1 = store.promote_recipe(candidates[0])
    r2 = store.promote_recipe(candidates[0])
    assert r1.recipe_id == r2.recipe_id
    assert len(store.get_learned_recipes()) == 1
