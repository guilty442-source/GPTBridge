from __future__ import annotations

from pathlib import Path

from file_sorter.application.cli import keyword_matches, resolve_destination_dir
from file_sorter.infrastructure.sorter_engine import load_profile


def test_ascii_keyword_matching_uses_word_boundaries() -> None:
    assert keyword_matches("annual report 2026", "report")
    assert not keyword_matches("reporting 2026", "report")
    assert keyword_matches("偶像_演唱會", "偶像")


def test_destination_must_be_existing_direct_child(tmp_path: Path) -> None:
    target = tmp_path / "inbox"
    destination = target / "reports"
    destination.mkdir(parents=True)
    assert resolve_destination_dir(target.resolve(), "reports") == destination.resolve()


def test_new_profile_automation_defaults_on(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    profile = load_profile(target, state_root=isolated_sorter_state)
    assert profile.enabled is True
    assert profile.duplicate_trash_enabled is False
