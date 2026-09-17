"""Merge/release recovery anchors (task §22).

Before any merge into ``main`` a governed recovery ref is created:

    refs/gptbridge/recovery/<yyyymmddThhmmssZ>/<queue_id>  ->  pre-merge HEAD

The ref is created through the governed ``update-ref`` path (Tier-2,
audited).  It is a safety anchor only — actual recovery stays a Tier-2/3
classified operation; ``reset --hard`` is never used automatically.
Refs are listed/retired through governance, never auto-deleted.

Optional ``git bundle`` checkpoints under ``E:\\GPTBridge-backup\\git``
are available for releases — never per ordinary commit.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from .git_repository import GitRepository

RECOVERY_PREFIX = "refs/gptbridge/recovery/"
BACKUP_ROOT = Path("E:/GPTBridge-backup/git")


def create_recovery_ref(
    repo: GitRepository,
    queue_id: str,
    *,
    target: str = "main",
    actor: str = "governance/workspace-sync",
) -> str:
    """Point a recovery ref at the current ``target`` HEAD; return the ref."""
    from .capability_gate import execute_system_safe

    head = repo.run(["rev-parse", f"refs/heads/{target}"])
    if head.returncode != 0 or not head.stdout.strip():
        return ""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in queue_id)
    ref = f"{RECOVERY_PREFIX}{stamp}/{safe_id}"
    gate = execute_system_safe(
        ["update-ref", ref, head.stdout.strip()],
        actor=actor, repo_path=repo.path,
    )
    result = gate.execution_result
    if gate.allowed is False or result is None:
        return ""
    return ref if result.returncode == 0 else ""


def list_recovery_refs(repo: GitRepository) -> list[dict[str, str]]:
    result = repo.run(
        ["for-each-ref", RECOVERY_PREFIX, "--format=%(refname) %(objectname)"]
    )
    refs: list[dict[str, str]] = []
    for line in (result.stdout or "").splitlines():
        name, _, sha = line.partition(" ")
        if name.strip():
            refs.append({"ref": name.strip(), "sha": sha.strip()})
    return refs


def create_bundle(
    repo: GitRepository,
    destination: str | Path | None = None,
    *,
    refs: Iterable[str] = ("main",),
    actor: str = "governance/release",
) -> Path | None:
    """Write a release-checkpoint bundle; never for ordinary commits."""
    from .capability_gate import execute_system_safe

    dest_dir = BACKUP_ROOT
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    head = repo.head()[:12] or "unknown"
    path = Path(destination) if destination else dest_dir / f"gptbridge-{stamp}-{head}.bundle"
    gate = execute_system_safe(
        ["bundle", "create", str(path), *refs],
        actor=actor, repo_path=repo.path,
    )
    result = gate.execution_result
    if gate.allowed is False or result is None:
        return None
    return path if result.returncode == 0 else None


__all__ = [
    "BACKUP_ROOT",
    "RECOVERY_PREFIX",
    "create_bundle",
    "create_recovery_ref",
    "list_recovery_refs",
]
