"""Git disaster-recovery diagnosis mixin (A185 split).

Extracted from ``disaster_recovery.py`` (source-size contract): fsck
integrity diagnosis, revision matrix, interrupted-operation detection
(§127-129/§149-150 — never auto-remove ``index.lock``), audit/registry/
hook health and per-worktree integrity inspection.  Read-only surfaces.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import audit_chain
from .disaster_recovery_types import (
    AUTOMATION_STATE_RELATIVE,
    RepositoryDiagnosis,
    WorktreeIntegrity,
    _FSCK_PATTERNS,
)


class DRDiagnoseMixin:
    """Diagnosis/inspection surface — never mutates repository state."""

    # ------------------------------------------------------------------
    # integrity diagnosis (122-124)
    # ------------------------------------------------------------------

    def diagnose_repository(self, *, deep: bool = False) -> RepositoryDiagnosis:
        if not deep:
            return RepositoryDiagnosis(state="UNKNOWN", raw_excerpt=["deep=false (fast path never runs fsck)"])
        result = self._git(["fsck", "--no-progress", "--connectivity-only"], timeout=600.0)
        output = f"{result.stdout}\n{result.stderr}"
        diagnosis = RepositoryDiagnosis(state="HEALTHY")
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            matched = False
            for bucket, pattern in _FSCK_PATTERNS:
                if pattern.search(stripped):
                    matched = True
                    if bucket == "CORRUPT":
                        diagnosis.state = "CORRUPT"
                        if "broken" in stripped.casefold():
                            diagnosis.broken_refs.append(stripped)
                        elif "missing" in stripped.casefold():
                            diagnosis.missing.append(stripped)
                        else:
                            diagnosis.corrupt.append(stripped)
                    elif diagnosis.state != "CORRUPT":
                        diagnosis.state = "WARN"
                        if "dangling" in stripped.casefold():
                            diagnosis.dangling.append(stripped)
                        else:
                            diagnosis.unreachable.append(stripped)
                    break
            if not matched and len(diagnosis.raw_excerpt) < 40:
                diagnosis.raw_excerpt.append(stripped)
        if result.returncode != 0 and diagnosis.state == "HEALTHY":
            diagnosis.state = "UNKNOWN"
        return diagnosis

    def revision_matrix(self) -> dict[str, Any]:
        local = self.ref_value("refs/heads/main")
        origin = self.ref_value("refs/remotes/origin/main")
        matrix: dict[str, Any] = {
            "local": local,
            "origin": origin,
            "classification": "UNKNOWN",
            "ancestry": {},
            "missing_commits": {},
        }
        refs = {"local": local, "origin": origin}
        for name, sha in refs.items():
            matrix["ancestry"][name] = {
                other: bool(sha and other_sha and sha != other_sha and self._is_ancestor(sha, other_sha))
                for other, other_sha in refs.items()
                if other != name
            }
        present = {name: sha for name, sha in refs.items() if sha}
        if len(present) < len(refs):
            # incomplete evidence: never guess divergence from a missing ref
            matrix["classification"] = "UNKNOWN"
        elif len(set(present.values())) == 1:
            matrix["classification"] = "ALL_EQUAL"
        elif self._is_ancestor(local, origin):
            matrix["classification"] = "ORIGIN_AHEAD"
        elif self._is_ancestor(origin, local):
            matrix["classification"] = "LOCAL_AHEAD"
        else:
            matrix["classification"] = "LOCAL_DIVERGED"
        for name, sha in present.items():
            counts = self._try(["rev-list", "--count", f"refs/heads/main..{sha}"])
            matrix["missing_commits"][name] = int(counts or 0) if counts.isdigit() else 0
        return matrix

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._git(["merge-base", "--is-ancestor", ancestor, descendant])
        return result.returncode == 0

    # ------------------------------------------------------------------
    # interrupted operations (127-129, 149-150)
    # ------------------------------------------------------------------

    def interrupted_state(self) -> dict[str, Any]:
        git_dir = self._worktree_git_dir()
        state = {
            "index_lock": (git_dir / "index.lock").exists(),
            "index_lock_age_seconds": None,
            "merge_head": (git_dir / "MERGE_HEAD").exists(),
            "cherry_pick_head": (git_dir / "CHERRY_PICK_HEAD").exists(),
            "revert_head": (git_dir / "REVERT_HEAD").exists(),
            "rebase_merge": (git_dir / "rebase-merge").exists(),
            "rebase_apply": (git_dir / "rebase-apply").exists(),
            "commit_editmsg": (git_dir / "COMMIT_EDITMSG").exists(),
        }
        lock = git_dir / "index.lock"
        if lock.exists():
            try:
                state["index_lock_age_seconds"] = round(time.time() - lock.stat().st_mtime, 1)
            except OSError:
                pass
        state["state"] = (
            "INTERRUPTED" if any(
                state[key]
                for key in ("merge_head", "cherry_pick_head", "revert_head", "rebase_merge", "rebase_apply")
            ) else ("STALE_INDEX_LOCK_CANDIDATE" if state["index_lock"] else "CLEAN")
        )
        state["note"] = "never auto-remove index.lock; governance decides (§128)"
        return state

    def _worktree_git_dir(self) -> Path:
        dot_git = self.root / ".git"
        if dot_git.is_file():
            try:
                raw = dot_git.read_text(encoding="utf-8").strip()
                if raw.startswith("gitdir:"):
                    return Path(raw[7:].strip())
            except OSError:
                pass
        return dot_git

    def audit_ledger_health(self) -> dict[str, Any]:
        """Detect a partial trailing record without truncating anything."""
        path = Path(audit_chain._chain_dir()) / "chain_state.json"
        health: dict[str, Any] = {"ledger": str(path), "state": "UNKNOWN", "last_complete_sequence": 0}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            health["last_complete_sequence"] = int(payload.get("next_sequence", 1)) - 1
            health["last_hash"] = str(payload.get("last_hash") or "")
            health["state"] = "HEALTHY"
        except FileNotFoundError:
            health["state"] = "UNKNOWN"
        except (OSError, ValueError):
            health["state"] = "CORRUPT"
        return health

    def registry_health(self) -> dict[str, Any]:
        path = self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "registry.json"
        health: dict[str, Any] = {"path": str(path), "state": "UNKNOWN"}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            health["state"] = "HEALTHY"
            health["children"] = len(payload.get("children") or [])
        except FileNotFoundError:
            health["state"] = "MISSING"
        except (OSError, ValueError):
            health["state"] = "CORRUPT"
        return health

    def hook_integrity(self) -> dict[str, Any]:
        current = self.hook_digest()
        state_path = self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "hook-integrity.json"
        previous: dict[str, Any] = {}
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        expected = str(previous.get("hook_digest") or "")
        state = "HEALTHY"
        if expected and expected != current:
            state = "FAILED"
        elif not expected:
            state = "UNKNOWN"
        return {"state": state, "hook_digest": current, "expected": expected}

    # ------------------------------------------------------------------
    # worktrees (130-131)
    # ------------------------------------------------------------------

    def worktree_integrity(self) -> list[WorktreeIntegrity]:
        results: list[WorktreeIntegrity] = []
        porcelain = self._try(["worktree", "list", "--porcelain"])
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in porcelain.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key.strip()] = value.strip()
        if current:
            entries.append(current)
        for entry in entries:
            path = entry.get("worktree", "")
            integrity = self._inspect_worktree(path, entry)
            results.append(integrity)
        return results
    def _inspect_worktree(self, path: str, entry: dict[str, str]) -> WorktreeIntegrity:
        if not path:
            return WorktreeIntegrity(path="", state="ORPHANED", detail="worktree path missing in listing")
        target = Path(path)
        path = str(target)
        if not target.exists():
            return WorktreeIntegrity(path=path, state="MISSING_PATH")
        if entry.get("bare") == "bare" or entry.get("detached") == "detached":
            # bare common repo / detached worktree: only basic checks
            pass
        dot_git = target / ".git"
        if not dot_git.exists():
            return WorktreeIntegrity(path=path, state="BROKEN_GITFILE", detail=".git missing")
        if dot_git.is_file():
            raw = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if not raw.startswith("gitdir:"):
                return WorktreeIntegrity(path=path, state="BROKEN_GITFILE", detail="gitdir pointer invalid")
            git_dir = Path(raw[7:].strip())
            if not git_dir.exists():
                return WorktreeIntegrity(path=path, state="BROKEN_GITFILE", detail="gitdir target missing")
        dirty = False
        from .git_repository import GitRepository

        inspector = GitRepository(target)
        try:
            status = inspector.run(["status", "--porcelain"], timeout=30.0)
            if status.returncode == 0:
                dirty = bool(status.stdout.strip())
        except (OSError, PermissionError):
            return WorktreeIntegrity(path=path, state="BROKEN_INDEX", detail="git status failed")
        head = ""
        branch = entry.get("branch", "").replace("refs/heads/", "")
        try:
            resolve = inspector.run(["rev-parse", "HEAD"], timeout=30.0)
            if resolve.returncode != 0:
                return WorktreeIntegrity(path=path, state="BROKEN_HEAD", detail="HEAD unresolved")
            head = resolve.stdout.strip()
        except (OSError, PermissionError):
            return WorktreeIntegrity(path=path, state="BROKEN_HEAD", detail="HEAD check failed")
        return WorktreeIntegrity(path=path, state="HEALTHY", branch=branch, head=head, dirty=dirty)


__all__ = ["DRDiagnoseMixin"]
