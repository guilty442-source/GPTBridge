"""Fault-injection repository factory + safety guard (A185 split).

Extracted from ``fault_injection.py`` (source-size contract): the
``gptbridge-test-repo`` marker contract, §219/§306 production-refusal
guard, deterministic §220-221 ``create_test_repository`` fixture builder
and its ``TestRepoFixture`` record.  Every injector operates exclusively
on a fixture produced here — injecting into the real repository or any
origin is refused at construction.

Layout (§219)::

    TempRoot/
      main/            primary worktree (branch: main)
      worktrees/wNNN/  worker worktrees (branch: wNNN)
      audit/           audit store
      recovery/        recovery bundles
      registry/        worker registry
      logs/            test logs
      gptbridge-test-repo   safety marker
      manifest.json    seed / versions / test_run_id (§221)
"""
from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_TEST_MARKER = "gptbridge-test-repo"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fault Injection",
    "GIT_AUTHOR_EMAIL": "fault@test.local",
    "GIT_COMMITTER_NAME": "Fault Injection",
    "GIT_COMMITTER_EMAIL": "fault@test.local",
    # Fixed dates keep seeded fixtures bit-for-bit reproducible (§221).
    "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+00:00",
}


class ProductionRepoError(RuntimeError):
    """Raised when fault injection targets a non-test repository."""


class InvariantViolation(RuntimeError):
    """Raised by ``assert_git_invariants(fail_fast=True)``."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("invariant violations: " + "; ".join(violations))


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_GIT_ENV}
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def git_version() -> str:
    out = subprocess.run(
        ["git", "--version"], capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return (out.stdout or "").strip()


def canonical_path(path: str | Path) -> str:
    """§309 — case-insensitive canonical form for path comparison."""
    return os.path.normcase(os.path.normpath(str(Path(path).resolve())))


# ---------------------------------------------------------------------------
# Safety guard (§219, §306)
# ---------------------------------------------------------------------------


def is_test_repository(root: str | Path) -> bool:
    """True only for marker-bearing roots under a temp/test tree."""
    root_path = Path(root).resolve()
    marker = root_path / _TEST_MARKER
    git_marker = root_path / ".git" / _TEST_MARKER
    if not (marker.is_file() or git_marker.is_file()):
        return False
    temp_root = Path(tempfile.gettempdir()).resolve()
    canonical = canonical_path(root_path)
    if canonical.startswith(canonical_path(temp_root)):
        return True
    parts = {p.lower() for p in root_path.parts}
    return bool(parts & {"tmp", "temp", "test", "tests", "fixtures"})


def assert_test_repository(root: str | Path) -> Path:
    """§306 — refuse anything that is not a disposable test repository.

    Hard-blocks the real project root, its ``.git`` and any path equal to
    or containing it, regardless of markers.
    """
    root_path = Path(root).resolve()
    real = canonical_path(_PROJECT_ROOT)
    target = canonical_path(root_path)
    if target == real or target.startswith(real + os.sep) or \
            real.startswith(target + os.sep):
        raise ProductionRepoError(
            f"refusing fault injection on production path: {root_path}")
    if not is_test_repository(root_path):
        raise ProductionRepoError(
            f"missing {_TEST_MARKER} marker / non-test root: {root_path}")
    return root_path


# ---------------------------------------------------------------------------
# Repository factory (§220-221)
# ---------------------------------------------------------------------------


@dataclass
class TestRepoFixture:
    """A disposable governed repository tree."""
    root: Path
    test_run_id: str
    seed: int
    main: Path
    worktrees_dir: Path
    audit_dir: Path
    recovery_dir: Path
    registry_dir: Path
    logs_dir: Path
    workers: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: Path = Path()

    def worker(self, index: int) -> dict[str, Any]:
        return self.workers[index]

    def write_manifest(self, extra: Optional[dict[str, Any]] = None) -> None:
        manifest = {
            "test_run_id": self.test_run_id,
            "seed": self.seed,
            "git_version": git_version(),
            "os": platform.platform(),
            "python_version": platform.python_version(),
            "workers": self.workers,
            **(extra or {}),
        }
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")


def create_test_repository(
    base_dir: str | Path,
    *,
    seed: int = 0,
    test_run_id: str = "",
    commit_count: int = 1,
    branch_count: int = 0,
    worktree_count: int = 0,
    file_count: int = 1,
    conflict_pattern: Optional[str] = None,
    origin_mirror: bool = False,
    audit_enabled: bool = True,
    hooks_enabled: bool = True,
) -> TestRepoFixture:
    """Build a deterministic governed test repository (§220-221)."""
    rng = random.Random(seed)
    root = Path(base_dir).resolve()
    run_id = test_run_id or uuid.uuid4().hex[:12]
    root.mkdir(parents=True, exist_ok=True)

    main = root / "main"
    worktrees_dir = root / "worktrees"
    audit_dir = root / "audit"
    recovery_dir = root / "recovery"
    registry_dir = root / "registry"
    logs_dir = root / "logs"
    for d in (main, worktrees_dir, audit_dir, recovery_dir,
              registry_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Main repository.
    _git(root, "init", "-b", "main", str(main))
    _git(main, "config", "user.name", "Fault Injection")
    _git(main, "config", "user.email", "fault@test.local")
    for i in range(max(1, file_count)):
        (main / f"file-{i}.txt").write_text(
            f"seed={seed} file={i} v0\n" + "\n".join(
                f"line-{j}-{rng.randint(0, 10**6)}" for j in range(8)
            ) + "\n", encoding="utf-8")
    for c in range(max(1, commit_count)):
        (main / f"file-{c % max(1, file_count)}.txt").write_text(
            f"seed={seed} file={c % max(1, file_count)} v{c}\n"
            + "\n".join(f"line-{j}-{rng.randint(0, 10**6)}"
                        for j in range(8)) + "\n", encoding="utf-8")
        _git(main, "add", "-A")
        _git(main, "commit", "-m", f"seed-{seed} commit {c}")

    # Extra branches on main (non-worktree).
    for b in range(branch_count):
        _git(main, "branch", f"topic-{b}")

    # Optional origin mirror.
    if origin_mirror:
        origin = root / "origin.git"
        _git(root, "init", "--bare", str(origin))
        _git(main, "remote", "add", "origin", str(origin))
        _git(main, "push", "origin", "main")

    # Worker worktrees w001..wNNN, one unique branch each.
    workers: list[dict[str, Any]] = []
    for w in range(1, worktree_count + 1):
        name = f"w{w:03d}"
        branch = name
        path = worktrees_dir / name
        _git(main, "worktree", "add", str(path), "-b", branch, "main")
        workers.append({
            "worker_id": name, "branch": branch,
            "worktree": str(path), "watcher_pid": None,
            "commit_cursor": 0,
        })

    # Safety marker + manifest (§219, §221, §306).
    (root / _TEST_MARKER).write_text(run_id, encoding="utf-8")
    (main / ".git" / _TEST_MARKER).write_text(run_id, encoding="utf-8")

    fixture = TestRepoFixture(
        root=root, test_run_id=run_id, seed=seed, main=main,
        worktrees_dir=worktrees_dir,
        audit_dir=audit_dir, recovery_dir=recovery_dir,
        registry_dir=registry_dir, logs_dir=logs_dir,
        workers=workers,
        manifest_path=root / "manifest.json",
    )
    fixture.write_manifest()
    (registry_dir / "registry.json").write_text(
        json.dumps({"generation": 1, "workers": workers}, indent=2),
        encoding="utf-8")
    (registry_dir / "registry.previous").write_text(
        (registry_dir / "registry.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    (audit_dir / "audit.jsonl").write_text("", encoding="utf-8")
    return fixture


__all__ = [
    "InvariantViolation", "ProductionRepoError", "TestRepoFixture",
    "assert_test_repository", "canonical_path", "create_test_repository",
    "git_version", "is_test_repository", "_GIT_ENV", "_TEST_MARKER",
    "_PROJECT_ROOT", "_git",
]
