"""Tests for the per-worktree automatic self-commit service (A53/E39).

These tests validate the single-shot commit core and its guards against an
isolated throwaway git repository: they never touch the real worktrees.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from governance_rule.execution.git_tiers.self_commit import (
    build_commit_message,
    operation_in_progress,
    run_once,
)
from governance_rule.execution.git_tiers.git_repository import GitRepository
from governance_rule.execution.git_tiers.worktree_manager import WorktreeManager


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture()
def scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "worktree"
    repo.mkdir()
    _git(repo, "init", "-b", "feature")
    _git(repo, "config", "user.name", "Self-Commit Test")
    _git(repo, "config", "user.email", "self-commit@test.local")
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_clean_repo_is_skipped(scratch_repo: Path) -> None:
    assert run_once(scratch_repo) == "clean"


def test_tracked_change_is_committed(scratch_repo: Path) -> None:
    (scratch_repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    assert run_once(scratch_repo) == "committed"
    log = _git(scratch_repo, "log", "-1", "--pretty=%s").stdout.strip()
    assert log.startswith("auto-commit(feature):")
    leftover = list(scratch_repo.rglob("self-commit-msg.txt"))
    assert leftover == []


def test_untracked_new_file_is_committed(scratch_repo: Path) -> None:
    (scratch_repo / "new_feature.py").write_text("print('hi')\n", encoding="utf-8")
    assert run_once(scratch_repo) == "committed"
    listed = _git(scratch_repo, "ls-files").stdout.splitlines()
    assert "new_feature.py" in listed


def test_ignored_file_is_not_committed(scratch_repo: Path) -> None:
    (scratch_repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(scratch_repo, "add", ".gitignore")
    _git(scratch_repo, "commit", "-m", "ignore build")
    (scratch_repo / "build").mkdir()
    (scratch_repo / "build" / "out.bin").write_bytes(b"\x00\x01")
    assert run_once(scratch_repo) == "clean"


def test_merge_in_progress_skips_commit(scratch_repo: Path) -> None:
    _git(scratch_repo, "checkout", "-b", "side")
    (scratch_repo / "tracked.txt").write_text("side\n", encoding="utf-8")
    _git(scratch_repo, "commit", "-am", "side change")
    _git(scratch_repo, "checkout", "feature")
    _git(scratch_repo, "merge", "--no-commit", "--no-ff", "side")
    assert _git(scratch_repo, "rev-parse", "--verify", "MERGE_HEAD").returncode == 0
    repo = GitRepository(scratch_repo)
    assert operation_in_progress(repo) is True
    assert run_once(scratch_repo) == "in-progress"
    _git(scratch_repo, "merge", "--abort")


def test_missing_identity_is_reported(scratch_repo: Path) -> None:
    _git(scratch_repo, "config", "--unset", "user.name")
    (scratch_repo / "tracked.txt").write_text("v9\n", encoding="utf-8")
    assert run_once(scratch_repo).startswith("error:")
    assert run_once(scratch_repo) == "error:missing-identity"


def test_build_commit_message_lists_files() -> None:
    msg = build_commit_message("ui", {"M src/a.ts": "M", "?? new.txt": "??"})
    assert "auto-commit(ui): 2 file(s) updated" in msg
    assert "- [M src/a.ts] M" in msg
    assert "Generated with" in msg


def test_worktree_discovery_works(scratch_repo: Path) -> None:
    joined = scratch_repo / "joined"
    joined.mkdir()
    _git(scratch_repo, "worktree", "add", str(joined), "-b", "second")
    manager = WorktreeManager(GitRepository(scratch_repo))
    paths = [wt["path"].replace("/", "\\") for wt in manager.list_worktrees()]
    assert str(joined.resolve()) in paths