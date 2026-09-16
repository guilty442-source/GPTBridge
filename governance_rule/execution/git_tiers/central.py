"""Central bare repository authority (task §15, A375 release topology).

``E:\\GPTBridge.git`` is the governed local integration receiver:
    worker branch -> main integration worktree -> central bare -> origin.

This module provides *verification and state* capability only — the push
stage stays disabled (``push=False``) unless explicitly enabled by a
governed caller.  Three revisions are always recorded explicitly and never
assumed equal:

    local_main_sha   refs/heads/main            (integration worktree)
    central_main_sha refs/heads/main @ central  (bare receiver)
    origin_main_sha  refs/remotes/origin/main   (remote tracking)

Sync states: IN_SYNC | LOCAL_AHEAD | CENTRAL_AHEAD | ORIGIN_AHEAD |
DIVERGED | MISSING_REF.  DIVERGED never triggers automatic merge, reset
or force-push — it is reported and the cycle stops.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .git_repository import GitRepository

SYNC_STATES = frozenset(
    {
        "IN_SYNC",
        "LOCAL_AHEAD",
        "CENTRAL_AHEAD",
        "ORIGIN_AHEAD",
        "DIVERGED",
        "MISSING_REF",
    }
)
CENTRAL_REMOTE = "central"
ORIGIN_REMOTE = "origin"
MAIN_REF = "refs/heads/main"


def central_path(root: str | Path) -> Path:
    """The canonical central bare repo: sibling ``GPTBridge.git``."""
    return Path(root).resolve().parent / "GPTBridge.git"


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


def tri_state(
    root: str | Path, *, live_remote: bool = False
) -> dict[str, Any]:
    """Resolve the three main revisions and classify the sync state."""
    repo = GitRepository(root)
    central_dir = central_path(root)
    local_main_sha = _sha(repo, MAIN_REF)

    central_main_sha = None
    central_exists = (central_dir / "HEAD").is_file()
    if central_exists:
        central_repo = GitRepository(central_dir)
        central_main_sha = _sha(central_repo, MAIN_REF)

    if live_remote:
        result = repo.run(["ls-remote", ORIGIN_REMOTE, MAIN_REF])
        line = (result.stdout or "").split()
        origin_main_sha = line[0] if result.returncode == 0 and line else None
    else:
        origin_main_sha = _sha(repo, f"refs/remotes/{ORIGIN_REMOTE}/main")

    relations = {
        "local_vs_central": _pairwise(repo, local_main_sha, central_main_sha),
        "local_vs_origin": _pairwise(repo, local_main_sha, origin_main_sha),
        "central_vs_origin": (
            _pairwise(GitRepository(central_dir), central_main_sha, origin_main_sha)
            if central_exists and origin_main_sha
            else ("missing" if not central_exists else _pairwise(repo, central_main_sha, origin_main_sha))
        ),
    }

    missing = [
        name
        for name, sha in (
            ("local", local_main_sha),
            ("central", central_main_sha),
            ("origin", origin_main_sha),
        )
        if not sha
    ]
    if missing:
        state = "MISSING_REF"
    elif "diverged" in relations.values():
        state = "DIVERGED"
    elif all(rel == "equal" for rel in relations.values()):
        state = "IN_SYNC"
    elif relations["local_vs_central"] == "ahead" or relations[
        "local_vs_origin"
    ] == "ahead":
        state = "LOCAL_AHEAD"
    elif relations["local_vs_central"] == "behind":
        state = "CENTRAL_AHEAD"
    elif relations["local_vs_origin"] == "behind":
        state = "ORIGIN_AHEAD"
    else:
        state = "DIVERGED"

    return {
        "state": state,
        "local_main_sha": local_main_sha,
        "central_main_sha": central_main_sha,
        "origin_main_sha": origin_main_sha,
        "missing": missing,
        "relations": relations,
        "central_path": str(central_dir),
        "central_exists": central_exists,
        "live_remote": live_remote,
    }


def evaluate_push_gates(root: str | Path) -> dict[str, Any]:
    """Evaluate the governed push sequence WITHOUT pushing.

    Order (each gate must pass before the next would run):
      main clean -> governance audit PASS -> central/main ancestor check
      -> [push central] -> verify central/main == local main
      -> origin/main ancestor check -> [push origin] -> fetch/verify
      -> tri-state re-verification.

    With push disabled this reports gate readiness only.
    """
    repo = GitRepository(root)
    gates: list[dict[str, Any]] = []

    dirty = bool(repo.status())
    gates.append({"gate": "main-clean", "passed": not dirty,
                  "detail": "working tree dirty" if dirty else ""})

    audit = subprocess.run(
        [sys.executable, "-m", "governance_rule.execution.audit"],
        cwd=Path(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    gates.append({"gate": "governance-audit", "passed": audit.returncode == 0,
                  "detail": (audit.stdout or "")[-200:]})

    state = tri_state(root)
    central_sha = state["central_main_sha"]
    local_sha = state["local_main_sha"]
    central_ancestor = bool(
        central_sha and local_sha and _is_ancestor(repo, central_sha, local_sha)
    )
    gates.append({"gate": "central-ancestor", "passed": central_ancestor,
                  "detail": "central missing or not ancestor" if not central_ancestor else ""})

    origin_sha = state["origin_main_sha"]
    origin_ancestor = bool(
        origin_sha and local_sha and _is_ancestor(repo, origin_sha, local_sha)
    )
    gates.append({"gate": "origin-ancestor", "passed": origin_ancestor,
                  "detail": "origin missing or not ancestor" if not origin_ancestor else ""})

    gates.append({"gate": "push-central", "passed": False, "skipped": True,
                  "detail": "push disabled (push=false)"})
    gates.append({"gate": "push-origin", "passed": False, "skipped": True,
                  "detail": "push disabled (push=false)"})

    return {
        "push_enabled": False,
        "gates": gates,
        "all_gates_passed": all(
            g["passed"] for g in gates if not g.get("skipped")
        ),
        "tri_state": state,
    }


def ensure_central(root: str | Path, *, actor: str = "governance/central") -> dict[str, Any]:
    """Create the central bare repo + ``central`` remote if absent."""
    repo = GitRepository(root)
    central_dir = central_path(root)
    created = False
    if not (central_dir / "HEAD").is_file():
        result = repo.run(
            ["init", "--bare", str(central_dir)],
            confirmed=True,
            actor=actor,
        )
        if result.returncode != 0:
            return {"created": False, "error": result.stderr.strip()[:200]}
        created = True
    remotes = repo.run(["remote"]).stdout.split()
    if CENTRAL_REMOTE not in remotes:
        repo.run(
            ["remote", "add", CENTRAL_REMOTE, str(central_dir)],
            confirmed=True,
            actor=actor,
        )
    return {"created": created, "central_path": str(central_dir)}


__all__ = [
    "CENTRAL_REMOTE",
    "MAIN_REF",
    "ORIGIN_REMOTE",
    "SYNC_STATES",
    "central_path",
    "ensure_central",
    "evaluate_push_gates",
    "tri_state",
]
