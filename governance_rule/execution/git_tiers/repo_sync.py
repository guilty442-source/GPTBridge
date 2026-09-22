"""Local repository sync state against the origin mirror (task §15).

There is no local bare receiver: the integration worktree ``main`` is the
single local delivery authority and ``origin`` is the remote mirror.

This module provides *verification and state* capability only — the push
stage stays disabled (``push=False``) unless explicitly enabled by a
governed caller.  Two revisions are always recorded explicitly and never
assumed equal:

    local_main_sha   refs/heads/main            (integration worktree)
    origin_main_sha  refs/remotes/origin/main   (remote tracking)

Sync states: IN_SYNC | LOCAL_AHEAD | ORIGIN_AHEAD | DIVERGED | MISSING_REF.
DIVERGED never triggers automatic merge, reset or force-push — it is
reported and the cycle stops.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .git_repository import GitRepository

SYNC_STATES = frozenset(
    {
        "IN_SYNC",
        "LOCAL_AHEAD",
        "ORIGIN_AHEAD",
        "DIVERGED",
        "MISSING_REF",
    }
)
ORIGIN_REMOTE = "origin"
MAIN_REF = "refs/heads/main"


def _sha(repo: GitRepository, ref: str) -> str | None:
    result = repo.run(["rev-parse", "--verify", "--quiet", ref])
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def _is_ancestor(repo: GitRepository, old: str, new: str) -> bool:
    return (
        repo.run(["merge-base", "--is-ancestor", old, new]).returncode == 0
    )


def _pairwise(
    repo: GitRepository, ours: str | None, theirs: str | None
) -> str:
    """Relation of ``ours`` to ``theirs``: equal|ahead|behind|diverged|missing."""
    if not ours or not theirs:
        return "missing"
    if ours == theirs:
        return "equal"
    if _is_ancestor(repo, theirs, ours):
        return "ahead"
    if _is_ancestor(repo, ours, theirs):
        return "behind"
    return "diverged"


def sync_state(
    root: str | Path, *, live_remote: bool = False
) -> dict[str, Any]:
    """Resolve the two main revisions and classify the sync state."""
    repo = GitRepository(root)
    local_main_sha = _sha(repo, MAIN_REF)

    if live_remote:
        result = repo.run(["ls-remote", ORIGIN_REMOTE, MAIN_REF])
        line = (result.stdout or "").split()
        origin_main_sha = line[0] if result.returncode == 0 and line else None
    else:
        origin_main_sha = _sha(repo, f"refs/remotes/{ORIGIN_REMOTE}/main")

    relations = {
        "local_vs_origin": _pairwise(repo, local_main_sha, origin_main_sha),
    }

    missing = [
        name
        for name, sha in (
            ("local", local_main_sha),
            ("origin", origin_main_sha),
        )
        if not sha
    ]
    if missing:
        state = "MISSING_REF"
    elif relations["local_vs_origin"] == "equal":
        state = "IN_SYNC"
    elif relations["local_vs_origin"] == "ahead":
        state = "LOCAL_AHEAD"
    elif relations["local_vs_origin"] == "behind":
        state = "ORIGIN_AHEAD"
    else:
        state = "DIVERGED"

    return {
        "state": state,
        "local_main_sha": local_main_sha,
        "origin_main_sha": origin_main_sha,
        "missing": missing,
        "relations": relations,
        "live_remote": live_remote,
    }


def evaluate_push_gates(root: str | Path) -> dict[str, Any]:
    """Evaluate the governed push sequence WITHOUT pushing.

    Order (each gate must pass before the next would run):
      main clean -> governance audit PASS -> origin/main ancestor check
      -> [push origin] -> fetch/verify -> sync-state re-verification.

    With push disabled this reports gate readiness only.
    """
    repo = GitRepository(root)
    gates: list[dict[str, Any]] = []

    dirty = bool(repo.status())
    gates.append({"gate": "main-clean", "passed": not dirty,
                  "detail": "working tree dirty" if dirty else ""})

    # Native audit engine is the gate authority (≤30 s budget, A537 /
    # §10.69-E③); the Python oracle only runs when the engine delegates
    # or ``GPTBRIDGE_AUDIT_GATE_ORACLE=1`` requests it explicitly.
    from governance_rule.execution.audit.native_audit_gate import (
        run_native_audit_gate,
    )

    native = run_native_audit_gate(Path(root))
    if native.status in ("fail", "timeout"):
        audit_passed, audit_detail = False, native.summary()
    elif native.status == "delegated" or os.environ.get(
        "GPTBRIDGE_AUDIT_GATE_ORACLE"
    ) == "1":
        audit = subprocess.run(
            [sys.executable, "-m", "governance_rule.execution.audit"],
            cwd=Path(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        audit_passed = audit.returncode == 0
        audit_detail = (audit.stdout or "")[-200:]
    else:
        audit_passed, audit_detail = True, native.summary()
    gates.append({"gate": "governance-audit", "passed": audit_passed,
                  "detail": audit_detail})

    # §10.69-C① mandatory test gate (bounded; configured under
    # flows.git-automation.push_gate in automation-flows.json).
    from .push_gate import mandatory_test_gate

    gates.append(mandatory_test_gate(root))

    state = sync_state(root)
    local_sha = state["local_main_sha"]
    origin_sha = state["origin_main_sha"]
    origin_ancestor = bool(
        origin_sha and local_sha and _is_ancestor(repo, origin_sha, local_sha)
    )
    gates.append({"gate": "origin-ancestor", "passed": origin_ancestor,
                  "detail": "origin missing or not ancestor" if not origin_ancestor else ""})

    gates.append({"gate": "push-origin", "passed": False, "skipped": True,
                  "detail": "push disabled (push=false)"})

    return {
        "push_enabled": False,
        "gates": gates,
        "all_gates_passed": all(
            g["passed"] for g in gates if not g.get("skipped")
        ),
        "sync_state": state,
    }


__all__ = [
    "MAIN_REF",
    "ORIGIN_REMOTE",
    "SYNC_STATES",
    "evaluate_push_gates",
    "sync_state",
]
