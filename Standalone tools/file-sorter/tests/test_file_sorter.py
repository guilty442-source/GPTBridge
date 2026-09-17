"""file-sorter test module (A57/E43)

source: main-system/tests/test_file_sorter.py
"""
from __future__ import annotations

import _sorter_test_boot  # noqa: F401  # sys.path bootstrap

from pathlib import Path

import pytest

from file_sorter.application.cli import (
    FileSorterError,
    keyword_matches,
    resolve_destination_dir,
)
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


def test_destination_rejects_nested_absolute_and_missing_folders(
    tmp_path: Path,
) -> None:
    target = tmp_path / "inbox"
    (target / "reports" / "2026").mkdir(parents=True)

    with pytest.raises(FileSorterError):
        resolve_destination_dir(target.resolve(), "reports/2026")
    with pytest.raises(FileSorterError):
        resolve_destination_dir(target.resolve(), r"reports\2026")
    with pytest.raises(FileSorterError):
        resolve_destination_dir(target.resolve(), "missing")
    with pytest.raises(FileSorterError):
        resolve_destination_dir(target.resolve(), str(tmp_path / "outside"))


def test_new_profile_automation_defaults_on(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    profile = load_profile(target, state_root=isolated_sorter_state)
    assert profile.enabled is True
    assert profile.duplicate_trash_enabled is False
