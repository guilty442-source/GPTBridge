"""Xingcheng codex-vs-implementation drift review tests (A137-A146).

Verifies 星澄 compares codex declarations with the live implementation,
reports drift as advisory-only review records, and displays the finding
on the 星澄 auxiliary user-facing surface (pending-action notice).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "shared-layer" / "src"))

from core_system.auto_action_policy import read_pending_actions  # noqa: E402
from core_system.codex_decision import SovereignRequest  # noqa: E402
from governance.sovereigns.xingcheng.learning_sub_sovereign import (  # noqa: E402
    LearningEvidenceSyncSubSovereign,
)
from governance.sovereigns.xingcheng_sovereign import XingchengSovereign  # noqa: E402


_CHILD_ID = "learning-evidence-sync-sub-sovereign"
_NOTICE_ID = "codex-drift-report"


class _App:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.xingcheng_sovereign = None


def _sovereign(tmp_path: Path) -> tuple[_App, XingchengSovereign]:
    app = _App(tmp_path)
    sovereign = XingchengSovereign(app)
    app.xingcheng_sovereign = sovereign
    return app, sovereign


def _xingcheng_findings(report: dict) -> list[dict]:
    return [f for f in report["drift"] if f.get("parent") == "星澄"]


def test_no_materialized_children_is_clean(tmp_path: Path) -> None:
    """A604 retired the sub-sovereign layer: with nothing materialized the
    星澄 subtree reports no drift (the learning child's codex row is
    retired, not an active declaration)."""
    _, sovereign = _sovereign(tmp_path)
    report = sovereign.compare_codex_implementation()
    assert _xingcheng_findings(report) == []


def test_retired_child_materialized_flagged_critical(tmp_path: Path) -> None:
    """A604: materializing an identity whose codex row is retired is
    critical drift (retired-identity-active)."""
    app, sovereign = _sovereign(tmp_path)
    child = LearningEvidenceSyncSubSovereign(app, parent=sovereign)
    sovereign._sub_sovereigns[_CHILD_ID] = child
    report = sovereign.compare_codex_implementation()
    assert any(
        f["type"] == "retired-identity-active"
        and f["child"] == _CHILD_ID
        and f["severity"] == "critical"
        for f in _xingcheng_findings(report)
    )
    assert report["clean"] is False


def test_unregistered_child_flagged_critical(tmp_path: Path) -> None:
    """A materialized child with no codex row is critical drift."""
    _, sovereign = _sovereign(tmp_path)
    sovereign._sub_sovereigns["phantom-sub-sovereign"] = object()
    report = sovereign.compare_codex_implementation()
    assert any(
        f["type"] == "unregistered-child"
        and f["child"] == "phantom-sub-sovereign"
        and f["severity"] == "critical"
        for f in _xingcheng_findings(report)
    )


def test_retired_child_flagged_regardless_of_claimed_parent(tmp_path: Path) -> None:
    """A604: a retired identity stays drift even when the instance claims
    a different parent — no active declaration exists to mismatch."""
    app, sovereign = _sovereign(tmp_path)
    child = LearningEvidenceSyncSubSovereign(app, parent=sovereign)
    child.parent_sovereign_id = "decision-sovereign"
    sovereign._sub_sovereigns[_CHILD_ID] = child
    report = sovereign.compare_codex_implementation()
    assert any(
        f["type"] == "retired-identity-active" and f["child"] == _CHILD_ID
        for f in _xingcheng_findings(report)
    )


def test_drift_notice_displayed_on_auxiliary_surface(tmp_path: Path) -> None:
    """The drift finding lands on the user-facing pending surface."""
    _, sovereign = _sovereign(tmp_path)
    sovereign._sub_sovereigns["phantom-sub-sovereign"] = object()
    report = sovereign.run_codex_drift_check()
    assert report["drift_count"] > 0
    actions = read_pending_actions(tmp_path)
    notice = [a for a in actions if a.get("action_id") == _NOTICE_ID]
    assert notice, "drift notice missing from auxiliary surface"
    assert notice[0]["kind"] == "codex-drift"
    assert notice[0]["detail"]["drift_count"] == report["drift_count"]
    assert notice[0]["detail"]["advisory"] is True
    assert notice[0]["expires_at"]


def test_drift_notice_removed_when_consistent(tmp_path: Path) -> None:
    """A clean comparison removes the stale drift notice."""
    _, sovereign = _sovereign(tmp_path)
    sovereign._display_drift_report(
        {
            "drift": [{"type": "missing-child", "severity": "warning"}],
            "drift_count": 1,
            "checked_at": "t0",
        }
    )
    assert any(
        a.get("action_id") == _NOTICE_ID
        for a in read_pending_actions(tmp_path)
    )
    sovereign._display_drift_report({"drift": [], "drift_count": 0})
    assert not any(
        a.get("action_id") == _NOTICE_ID
        for a in read_pending_actions(tmp_path)
    )


def test_review_intent_routes_and_records(tmp_path: Path) -> None:
    """review.codex-drift runs the check and stores an advisory record."""
    _, sovereign = _sovereign(tmp_path)

    async def _run() -> None:
        outcome = await sovereign.handle(
            SovereignRequest(
                intent="review.codex-drift",
                subject="codex-implementation",
                requester="星澄",
                payload={},
            )
        )
        assert outcome.accepted is True
        result = outcome.result
        assert result["advisory"] is True
        assert result["displayed_on"] == "xingcheng-auxiliary-surface"
        assert "drift" in result and "clean" in result
        assert any(
            record.get("kind") == "codex-drift"
            for record in sovereign._reviews.values()
        )

    asyncio.run(_run())


def test_drift_status_surface(tmp_path: Path) -> None:
    _, sovereign = _sovereign(tmp_path)
    assert sovereign.drift_status()["checked"] is False
    sovereign._sub_sovereigns["phantom-sub-sovereign"] = object()
    sovereign.run_codex_drift_check()
    status = sovereign.drift_status()
    assert status["checked"] is True
    assert status["drift_count"] > 0
    live = sovereign.live_status()
    assert live["codex_drift"]["checked"] is True
