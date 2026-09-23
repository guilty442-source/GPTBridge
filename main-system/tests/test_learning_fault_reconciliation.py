"""Learning-driven fault-message reconciliation tests.

Verifies the learning-evidence-sync sub-sovereign (A310/A322) absorbs
non-actionable fault evidence and eliminates the corresponding Xingcheng
pending-confirmation messages without touching actionable items.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "shared-layer" / "src"))

from governance.sovereigns.xingcheng.learning_sub_sovereign import (  # noqa: E402
    LearningEvidenceSyncSubSovereign,
)
from governance.sovereigns.xingcheng_sovereign import (  # noqa: E402
    XingchengSovereign,
)
from core_system.auto_action_policy import (  # noqa: E402
    read_pending_actions,
    remove_pending_actions,
)


class _App:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root


def _pending_path(root: Path) -> Path:
    return root / "main-system" / "runtime" / "state" / "pending-actions.json"


def _write_actions(root: Path, actions: list[dict]) -> None:
    path = _pending_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(actions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _fallback_action(action_id: str, *, expires_at: str = "") -> dict:
    return {
        "action_id": action_id,
        "kind": "repair",
        "summary": "STARTUP_CRASH (unknown)",
        "status": "awaiting-confirmation",
        "scope": "STARTUP_CRASH",
        "target": "STARTUP_CRASH",
        "proposed_method": "fallback",
        "risk": "unclassified",
        "expires_at": expires_at,
        "evidence_digest": "a" * 64,
        "detail": {
            "failure_code": "STARTUP_CRASH",
            "classified": {
                "error_type": "",
                "target_file": "",
                "action": "fallback",
                "diagnosis": {"error_type": "", "file": "", "action": "fallback"},
            },
        },
    }


def _actionable_action(action_id: str) -> dict:
    return {
        "action_id": action_id,
        "kind": "repair",
        "summary": "hot_reload_watcher.py import error",
        "status": "awaiting-confirmation",
        "scope": "IMPORT_ERROR",
        "target": "tasks.hot_reload_watcher",
        "proposed_method": "patch",
        "risk": "low",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        "detail": {
            "failure_code": "IMPORT_ERROR",
            "classified": {
                "error_type": "ImportError",
                "target_file": "main-system/src-core/tasks/hot_reload_watcher.py",
                "action": "patch",
                "diagnosis": {
                    "error_type": "ImportError",
                    "file": "main-system/src-core/tasks/hot_reload_watcher.py",
                    "action": "patch",
                },
            },
        },
    }


def test_reconcile_removes_only_non_actionable_messages(tmp_path: Path) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _write_actions(
        tmp_path,
        [
            _fallback_action("repair-expired-fallback", expires_at=past),
            _fallback_action("repair-fallback", expires_at=future),
            _actionable_action("repair-actionable"),
            {**_fallback_action("repair-confirmed"), "status": "confirmed"},
        ],
    )
    sovereign = LearningEvidenceSyncSubSovereign(_App(tmp_path))

    receipt = sovereign.reconcile_pending_fault_messages()

    assert receipt["ok"] is True
    assert set(receipt["removed"]) == {
        "repair-expired-fallback",
        "repair-fallback",
    }
    remaining = read_pending_actions(tmp_path)
    assert {item["action_id"] for item in remaining} == {
        "repair-actionable",
        "repair-confirmed",
    }
    assert receipt["remaining"] == 2
    assert all(item["reason"] for item in receipt["learned"])

    store_files = list((tmp_path / "main-system" / "data" / "automatic-repair").glob("*.sqlite3"))
    assert store_files, "learning store must persist the absorbed evidence"

    audit_path = (
        tmp_path
        / "main-system"
        / "runtime"
        / "state"
        / "learning-fault-reconciliation.jsonl"
    )
    entries = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["actor"] == "learning-evidence-sync-sub-sovereign"
    assert set(entry["removed"]) == set(receipt["removed"])
    assert entry["pending_before"] == 4 and entry["pending_after"] == 2


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    _write_actions(tmp_path, [_fallback_action("repair-fallback")])
    sovereign = LearningEvidenceSyncSubSovereign(_App(tmp_path))

    first = sovereign.reconcile_pending_fault_messages()
    second = sovereign.reconcile_pending_fault_messages()

    assert first["removed"] == ["repair-fallback"]
    assert second["removed"] == []
    assert second["remaining"] == 0


def test_non_actionable_outcomes_never_become_successful_remedies(
    tmp_path: Path,
) -> None:
    from tasks.repair_learning import (
        ErrorSignature,
        RepairLearner,
        RepairLearningStore,
        _normalize_error_signature,
    )

    store = RepairLearningStore(tmp_path / "learning")
    learner = RepairLearner(store)
    signature_hash = _normalize_error_signature("STARTUP_CRASH", "STARTUP_CRASH (unknown)")
    signature = ErrorSignature(
        signature_hash=signature_hash,
        error_class="STARTUP_CRASH",
        message_pattern="STARTUP_CRASH (unknown)",
        failure_code="STARTUP_CRASH",
    )
    suggestion = learner.suggest_remedy(signature)
    assert suggestion["suggested"] is False


def test_remove_pending_actions_rejects_non_pending_items(tmp_path: Path) -> None:
    _write_actions(
        tmp_path,
        [
            {**_fallback_action("repair-executing"), "status": "executing"},
            _fallback_action("repair-pending"),
        ],
    )
    removed = remove_pending_actions(
        tmp_path, ["repair-executing", "repair-pending"], actor="test"
    )
    assert removed == ["repair-pending"]
    assert [item["action_id"] for item in read_pending_actions(tmp_path)] == [
        "repair-executing"
    ]


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "A604 retired learning-evidence-sync-sub-sovereign: the "
        "delegation path fails closed (child-parent-mismatch) pending "
        "the convergence workstream registry switch — see "
        "governance_rule/execution/audit/convergence/"
        "a594-learning-command-regression-20260922.json"
    ),
)
async def test_reconcile_loop_eliminates_messages_automatically(
    tmp_path: Path,
) -> None:
    _write_actions(tmp_path, [_fallback_action("repair-fallback-auto")])
    # A485: the child never self-arms — 星澄 commands auto-learning via
    # the governed delegation path (learn.auto-start).
    app = _App(tmp_path)
    parent = XingchengSovereign(app)
    app.xingcheng_sovereign = parent
    sovereign = LearningEvidenceSyncSubSovereign(
        app, parent=parent, reconcile_interval=0.5
    )
    parent._sub_sovereigns["learning-evidence-sync-sub-sovereign"] = sovereign

    await sovereign.start()
    assert sovereign._reconcile_task is None
    armed = await parent.start_learning_automation()
    assert armed["commanded"] is True
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        while datetime.now(timezone.utc) < deadline and read_pending_actions(tmp_path):
            await asyncio.sleep(0.05)
        assert read_pending_actions(tmp_path) == []
        audit_path = (
            tmp_path
            / "main-system"
            / "runtime"
            / "state"
            / "learning-fault-reconciliation.jsonl"
        )
        entries = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert entries
        assert entries[0]["actor"] == "learning-evidence-sync-sub-sovereign"
        assert "repair-fallback-auto" in entries[0]["removed"]
        live = sovereign.live_status()
        assert live["reconcile_loop"] is True
        assert live["reconciliation"].get("at")
    finally:
        await sovereign.stop()
    assert sovereign._reconcile_task is None


@pytest.mark.asyncio
async def test_stop_is_safe_without_start(tmp_path: Path) -> None:
    sovereign = LearningEvidenceSyncSubSovereign(_App(tmp_path))
    await sovereign.stop()
    assert sovereign._reconcile_task is None


def test_reconcile_interval_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GPTBRIDGE_LEARNING_RECONCILE_INTERVAL", "0.25")
    sovereign = LearningEvidenceSyncSubSovereign(_App(tmp_path))
    assert sovereign._interval_seconds() == 0.25
    explicit = LearningEvidenceSyncSubSovereign(
        _App(tmp_path), reconcile_interval=7.5
    )
    assert explicit._interval_seconds() == 7.5
