"""Read-only merge-conflict preflight (task §19).

Runs *before* the real merge and never touches the main working tree:

    merge-base <target> <source>          — shared ancestry exists?
    diff --check <target>...<source>      — whitespace/conflict markers
    diff --name-only <target>...<source>  — touched paths
    touched-path overlap across queued workers -> conflict_risk
    git merge-tree --write-tree           — real tree-level dry run
                                           (objects only; no refs/index/
                                           working tree writes)

A LOW risk estimate is never a reason to skip the real Git merge
verification — the precheck is advisory sequencing evidence only.
"""
from __future__ import annotations

from typing import Any, Iterable

from .git_repository import GitRepository


def _touched_paths(repo: GitRepository, target: str, source: str) -> list[str]:
    result = repo.run(["diff", "--name-only", f"{target}...{source}"])
    if result.returncode != 0:
        return []
    return sorted(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )


def merge_tree_check(repo: GitRepository, target: str, source: str) -> dict[str, Any]:
    """Tree-level merge dry run; unsupported on old git -> skipped."""
    from .capability_gate import execute_system_safe

    gate = execute_system_safe(
        ["merge-tree", "--write-tree", target, source],
        actor="governance/merge-precheck", repo_path=repo.path,
    )
    result = gate.execution_result
    if gate.allowed is False or result is None:
        return {"supported": True, "clean": False,
                "detail": f"{gate.code}:{gate.detail}"[:300]}
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if "unknown" in stderr or "usage" in stderr or "not a git command" in stderr:
            return {"supported": False, "clean": None}
        return {"supported": True, "clean": False,
                "detail": result.stderr.strip()[:300]}
    # merge-tree --write-tree exits 0 for clean, 1 for conflicts (modern git)
    output = (result.stdout or "").strip().splitlines()
    return {"supported": True, "clean": True, "tree": output[0] if output else ""}


def pre_merge_check(
    repo: GitRepository,
    source: str,
    *,
    target: str = "main",
    other_touched: Iterable[Iterable[str]] = (),
) -> dict[str, Any]:
    """Full read-only preflight for merging ``source`` into ``target``."""
    report: dict[str, Any] = {"source": source, "target": target}

    merge_base = repo.run(["merge-base", target, source])
    report["merge_base"] = (merge_base.stdout or "").strip()
    report["has_ancestor"] = merge_base.returncode == 0 and bool(report["merge_base"])

    already = repo.run(["merge-base", "--is-ancestor", source, target])
    report["already_merged"] = already.returncode == 0

    whitespace = repo.run(["diff", "--check", f"{target}...{source}"])
    report["diff_check_clean"] = whitespace.returncode == 0
    report["diff_check_detail"] = (whitespace.stdout or whitespace.stderr or "")[:300]

    paths = _touched_paths(repo, target, source)
    report["touched_paths"] = paths

    overlap: list[str] = []
    mine = set(paths)
    for foreign in other_touched:
        overlap.extend(sorted(mine & set(foreign)))
    report["overlap"] = sorted(set(overlap))

    tree = merge_tree_check(repo, target, source)
    report["merge_tree"] = tree

    if not report["has_ancestor"]:
        risk = "HIGH"
    elif tree.get("clean") is False or overlap or not report["diff_check_clean"]:
        risk = "HIGH" if tree.get("clean") is False else "MEDIUM"
    elif not report["already_merged"] and len(paths) > 200:
        risk = "MEDIUM"
    else:
        risk = "LOW"
    report["conflict_risk"] = risk
    return report


__all__ = ["merge_tree_check", "pre_merge_check"]
