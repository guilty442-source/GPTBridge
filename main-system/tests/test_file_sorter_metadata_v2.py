from __future__ import annotations

from pathlib import Path

from file_sorter.infrastructure.sorter_engine import (
    _atomic_write_json,
    new_plan,
    prune_state,
    save_plan,
)


def test_prune_removes_expired_plans_but_keeps_journals_by_default(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    plan = new_plan(
        target,
        profile_id="target-12345678",
        rules_revision=0,
        quiet_seconds=0,
        operations=[],
        skipped=[],
    )
    plan.expires_at = "2000-01-01T00:00:00+00:00"
    plan_path = save_plan(plan, state_root=isolated_sorter_state)
    journal_path = isolated_sorter_state / "journals" / "12345678-journal.json"
    _atomic_write_json(
        journal_path,
        {
            "status": "committed",
            "created_at": "2000-01-01T00:00:00+00:00",
            "updated_at": "2000-01-01T00:00:00+00:00",
        },
    )

    default_result = prune_state(state_root=isolated_sorter_state)
    assert default_result["removed_plans"] == 1
    assert not plan_path.exists()
    assert journal_path.exists()

    retained_result = prune_state(
        state_root=isolated_sorter_state,
        journal_retention_days=30,
    )
    assert retained_result["removed_journals"] == 1
    assert not journal_path.exists()
