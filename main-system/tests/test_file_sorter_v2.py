from __future__ import annotations

from pathlib import Path

from file_sorter.application.cli import apply_organize_plan, preview_organize_files
from file_sorter.infrastructure.sorter_engine import (
    load_profile,
    undo_last_transaction,
)


def test_preview_apply_and_undo_round_trip(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    destination = target / "reports"
    destination.mkdir(parents=True)
    source = target / "annual reports.txt"
    source.write_text("important", encoding="utf-8")
    load_profile(target, state_root=isolated_sorter_state)

    plan = preview_organize_files(
        target,
        quiet_seconds=0,
        state_root=isolated_sorter_state,
    )
    assert len(plan.operations) == 1
    applied = apply_organize_plan(
        plan.plan_id,
        target_dir=target,
        state_root=isolated_sorter_state,
    )
    assert applied["ok"] is True
    assert not source.exists()
    assert (destination / source.name).exists()

    undone = undo_last_transaction(target, state_root=isolated_sorter_state)
    assert undone["ok"] is True
    assert source.exists()
    assert not (destination / source.name).exists()
