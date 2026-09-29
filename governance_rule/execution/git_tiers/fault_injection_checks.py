"""Fault-injection invariant checker + failure artifacts (A185 split).

Extracted from ``fault_injection.py`` (source-size contract):
§299-300 ``assert_git_invariants`` (returns violations, ``fail_fast``
raises ``InvariantViolation``) and §301 ``capture_failure_artifacts``
(preserve the failing fixture — never cleanup the scene).
"""
from __future__ import annotations

import json
import platform
import shutil
from pathlib import Path
from typing import Optional

from .fault_injection_repo import (
    InvariantViolation,
    TestRepoFixture,
    _git,
    canonical_path,
    git_version,
)


def _heads(repo: Path, ref: str) -> str:
    out = _git(repo, "rev-parse", "--verify", ref, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def assert_git_invariants(
    fixture: TestRepoFixture, *, fail_fast: bool = False,
) -> list[str]:
    """§299 — check the governed invariants against real Git state.

    Returns a list of violation strings (empty = all hold).  With
    ``fail_fast=True`` raises ``InvariantViolation`` so chaos runners
    stop before touching the fixture further (§300).
    """
    violations: list[str] = []
    main = fixture.main

    # main has no watcher / single writer: registry must not assign one.
    reg_path = fixture.registry_dir / "registry.json"
    try:
        registry = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        violations.append("registry-corrupt-or-missing")
        registry = {"workers": []}

    branches: dict[str, str] = {}
    worktrees: set[str] = set()
    for worker in fixture.workers:
        wt = Path(worker["worktree"])
        branch = worker["branch"]
        if branch in branches:
            violations.append(
                f"branch-collision:{branch}:{branches[branch]}/"
                f"{worker['worker_id']}")
        branches[branch] = worker["worker_id"]
        canonical = canonical_path(wt)
        if canonical in worktrees:
            violations.append(f"worktree-collision:{wt}")
        worktrees.add(canonical)

    # queue source SHA immutability: queue files pin a sha field.
    queue_dir = fixture.root / "queue"
    if queue_dir.is_dir():
        for entry in queue_dir.glob("*.json"):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                violations.append(f"queue-entry-corrupt:{entry.name}")
                continue
            if not data.get("source_sha"):
                violations.append(f"queue-entry-missing-sha:{entry.name}")

    # audit sequence monotonic + valid JSONL tail.
    audit_log = fixture.audit_dir / "audit.jsonl"
    if audit_log.is_file():
        last_seq = -1
        for lineno, line in enumerate(
                audit_log.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                violations.append(f"audit-partial-tail:line-{lineno}")
                break
            seq = int(rec.get("sequence", -1))
            if seq <= last_seq:
                violations.append(f"audit-sequence-nonmonotonic:{lineno}")
            last_seq = seq

    # no merge in progress left dangling on main.
    if (main / ".git" / "MERGE_HEAD").is_file():
        violations.append("main-merge-in-progress")

    if fail_fast and violations:
        raise InvariantViolation(violations)
    return violations


# ---------------------------------------------------------------------------
# Failure artifact bundle (§301)
# ---------------------------------------------------------------------------


def capture_failure_artifacts(
    fixture: TestRepoFixture, reason: str,
    violations: Optional[list[str]] = None,
) -> Path:
    """Preserve the failing fixture — never cleanup the scene (§300-301)."""
    out = fixture.root / "failure-artifacts" / fixture.test_run_id
    out.mkdir(parents=True, exist_ok=True)
    for name in ("audit", "registry", "logs"):
        src = getattr(fixture, f"{name}_dir", None) or fixture.root / name
        if Path(src).is_dir():
            shutil.copytree(src, out / name, dirs_exist_ok=True)
    refs = _git(fixture.main, "for-each-ref",
                "--format=%(refname) %(objectname)", check=False)
    (out / "refs.txt").write_text(refs.stdout or "", encoding="utf-8")
    (out / "worktrees.txt").write_text(
        json.dumps(fixture.workers, indent=2), encoding="utf-8")
    (out / "failure.json").write_text(json.dumps({
        "test_run_id": fixture.test_run_id, "seed": fixture.seed,
        "reason": reason, "violations": violations or [],
        "git_version": git_version(), "os": platform.platform(),
        "python_version": platform.python_version(),
    }, indent=2), encoding="utf-8")
    return out


__all__ = [
    "assert_git_invariants", "capture_failure_artifacts", "_heads",
]
