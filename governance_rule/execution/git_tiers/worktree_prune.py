"""Safe worktree prune (task §26).

``git worktree prune`` is never run blindly.  A dry-run lists candidates;
each candidate must pass the registry checks before it may be pruned:

  * worktree registry state is retired / path already gone or released
  * the branch (if known) has no unmerged commits
  * no active PID owns the checkout
  * no active claim covers it

Actual prune stays a Tier-2 governed operation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .claims import ClaimRegistry
from .git_repository import GitRepository
from .worktree_manager import WorktreeManager


def _candidate_paths(repo: GitRepository) -> list[str]:
    """Dry-run prune output → stale admin dir names under .git/worktrees."""
    result = repo.run(["worktree", "prune", "--dry-run", "--verbose"])
    names: list[str] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        # verbose lines look like: Removing worktrees/<id>: <reason>
        marker = "worktrees/"
        if marker in line:
            names.append(line.split(marker, 1)[1].split(":", 1)[0].strip())
    return names


def _worktree_gitdir(repo: GitRepository, admin_name: str) -> Path | None:
    git_dir_result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (git_dir_result.stdout or "").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo.path / common
    gitdir_file = common.resolve() / "worktrees" / admin_name / "gitdir"
    try:
        return Path(gitdir_file.read_text(encoding="utf-8").strip()).parent
    except OSError:
        return None


def prune_candidates(root: str | Path) -> dict[str, Any]:
    """Evaluate dry-run prune candidates against registry safety rules."""
    repo = GitRepository(root)
    manager = WorktreeManager(repo)
    registry_paths = {item["path"] for item in manager.list_worktrees()}
    claims = ClaimRegistry(root)
    active_claim_paths = {
        p for record in claims.active() for p in record.get("paths", [])
    }

    candidates: list[dict[str, Any]] = []
    safe: list[str] = []
    unsafe: list[dict[str, str]] = []
    for name in _candidate_paths(repo):
        worktree_path = _worktree_gitdir(repo, name)
        entry = {"admin_name": name, "worktree": str(worktree_path or "")}
        candidates.append(entry)
        reasons: list[str] = []
        if worktree_path is not None and str(worktree_path) in registry_paths:
            reasons.append("still-registered")
        if worktree_path is not None and worktree_path.is_dir():
            # path still exists and was not formally released
            reasons.append("path-present")
        norm = str(worktree_path or "").replace("\\", "/").casefold()
        if any(norm and (norm.startswith(p) or p.startswith(norm)) for p in active_claim_paths):
            reasons.append("active-claim")
        if reasons:
            entry["unsafe_reasons"] = reasons
            unsafe.append({"admin_name": name, "reasons": ",".join(reasons)})
        else:
            safe.append(name)
    return {"candidates": candidates, "safe": safe, "unsafe": unsafe}


def execute_prune(
    root: str | Path, *, actor: str = "governance/worktree-prune"
) -> dict[str, Any]:
    """Tier-2 prune — runs only when every dry-run candidate passed checks."""
    evaluation = prune_candidates(root)
    if evaluation["unsafe"]:
        return {
            "pruned": False,
            "reason": "unsafe-candidates",
            "unsafe": evaluation["unsafe"],
        }
    repo = GitRepository(root)
    from .capability_gate import execute_system_safe

    gate = execute_system_safe(
        ["worktree", "prune", "--verbose"], actor=actor, repo_path=repo.path,
    )
    result = gate.execution_result
    return {
        "pruned": bool(
            gate.allowed is not False
            and result is not None
            and result.returncode == 0
        ),
        "removed": evaluation["safe"],
        "detail": (
            f"{gate.code}:{gate.detail}"[:300] if result is None
            else (result.stdout or result.stderr or "")[:300]
        ),
    }


__all__ = ["execute_prune", "prune_candidates"]
