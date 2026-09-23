"""Tests for the per-generation shared Git snapshot (G101 / §3.2-§3.3).

Each test uses an isolated throwaway repository; the shared module-level
cache is cleared between tests.
"""
import subprocess
import time
from pathlib import Path

import pytest

from governance_rule.execution.git_tiers import generation_snapshot as gs


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


@pytest.fixture(autouse=True)
def _clear_cache():
    gs.invalidate_all()
    yield
    gs.invalidate_all()


@pytest.fixture()
def scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "worktree"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Snapshot Test")
    _git(repo, "config", "user.email", "snapshot@test.local")
    (repo / "main-system").mkdir()
    (repo / "main-system" / "a.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_snapshot_fields(scratch_repo: Path) -> None:
    snap = gs.generation_snapshot(scratch_repo)
    assert snap.head_oid
    assert snap.branch == "main"
    assert snap.dirty is False
    assert snap.fingerprint
    assert snap.generation
    assert snap.changed_paths == ()
    assert snap.affected_scope == ()


def test_dirty_paths_and_scope(scratch_repo: Path) -> None:
    repo = scratch_repo
    (repo / "native").mkdir()
    (repo / "native" / "core.c").write_text("x\n", encoding="utf-8")
    (repo / "Standalone tools").mkdir()
    (repo / "Standalone tools" / "tool-a").mkdir()
    (repo / "Standalone tools" / "tool-a" / "t.py").write_text(
        "x\n", encoding="utf-8"
    )
    (repo / "loose.txt").write_text("x\n", encoding="utf-8")
    # intent-to-add so porcelain reports file-level entries (untracked
    # directories otherwise collapse to "<dir>/" records)
    _git(repo, "add", "-N", ".")
    snap = gs.generation_snapshot(repo)
    assert snap.dirty is True
    assert set(snap.changed_paths) == {
        "native/core.c",
        "Standalone tools/tool-a/t.py",
        "loose.txt",
    }
    assert snap.affected_scope == (
        "(root)",
        "Standalone tools/tool-a",
        "native",
    )


def test_cache_reuse_within_ttl(scratch_repo: Path) -> None:
    snap1 = gs.generation_snapshot(scratch_repo, max_age_s=60)
    snap2 = gs.generation_snapshot(scratch_repo, max_age_s=60)
    assert snap2 is snap1


def test_probe_change_recaptures(scratch_repo: Path) -> None:
    snap1 = gs.generation_snapshot(scratch_repo, max_age_s=60)
    (scratch_repo / "main-system" / "a.py").write_text(
        "v2\n", encoding="utf-8"
    )
    _git(scratch_repo, "add", ".")
    _git(scratch_repo, "commit", "-m", "second")
    snap2 = gs.generation_snapshot(scratch_repo, max_age_s=60)
    assert snap2 is not snap1
    assert snap2.head_oid != snap1.head_oid


def test_notify_changed_forces_recapture(scratch_repo: Path) -> None:
    snap1 = gs.generation_snapshot(scratch_repo, max_age_s=60)
    gs.notify_changed(scratch_repo)
    snap2 = gs.generation_snapshot(scratch_repo, max_age_s=60)
    assert snap2 is not snap1


def test_ttl_zero_always_fresh(scratch_repo: Path) -> None:
    snap1 = gs.generation_snapshot(scratch_repo, max_age_s=0)
    snap2 = gs.generation_snapshot(scratch_repo, max_age_s=0)
    assert snap2 is not snap1


def test_dirty_without_git_change_ages_out(scratch_repo: Path) -> None:
    # A pure worktree edit never touches .git — the probe cannot see it;
    # the TTL bound is what recaptures it.
    snap1 = gs.generation_snapshot(scratch_repo, max_age_s=0.05)
    (scratch_repo / "main-system" / "a.py").write_text(
        "v2\n", encoding="utf-8"
    )
    same = gs.generation_snapshot(scratch_repo, max_age_s=60)
    assert same is snap1  # probe unchanged -> cached view
    time.sleep(0.06)
    fresh = gs.generation_snapshot(scratch_repo, max_age_s=0.05)
    assert fresh.dirty is True
    assert "main-system/a.py" in fresh.changed_paths


def test_not_a_worktree(tmp_path: Path) -> None:
    snap = gs.generation_snapshot(tmp_path / "nowhere")
    assert snap.dirty is False
    assert snap.changed_paths == ()


def test_scope_of_mapping() -> None:
    assert gs.scope_of("native/core/x.c") == "native"
    assert gs.scope_of("main-system/src-core/app.py") == "main-system"
    assert (
        gs.scope_of("Standalone tools/local-model/x.py")
        == "Standalone tools/local-model"
    )
    assert gs.scope_of(".worktrees/ui/src/a.ts") == ".worktrees/ui"
    assert gs.scope_of("AGENTS.md") == "(root)"


def test_staged_split(scratch_repo: Path) -> None:
    repo = scratch_repo
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    _git(repo, "add", "new.txt")
    (repo / "main-system" / "a.py").write_text("v2\n", encoding="utf-8")
    snap = gs.generation_snapshot(repo)
    assert "new.txt" in snap.staged_paths
    assert "main-system/a.py" in snap.unstaged_paths
    assert snap.status_map["new.txt"].startswith("A")
