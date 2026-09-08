"""Worktree manager for multi-AI-worker git layout."""
from __future__ import annotations

from .git_repository import GitRepository


class WorktreeManager:
    """Operate on git worktrees in a bare or main repository."""

    def __init__(
        self,
        repository: GitRepository | None = None,
        *,
        actor: str = "governance/worktree-manager",
    ) -> None:
        self._repo = repository or GitRepository()
        self._actor = actor

    def list_worktrees(self) -> list[dict[str, str]]:
        result = self._repo.run(["worktree", "list", "--porcelain"])
        if result.returncode != 0:
            return []

        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[len("worktree "):].strip()}
            elif line.startswith("HEAD "):
                current["head"] = line[len("HEAD "):].strip()
            elif line.startswith("branch "):
                current["branch"] = line[len("branch "):].strip()
            elif line.startswith("detached"):
                current["branch"] = "HEAD"
        if current:
            worktrees.append(current)
        return worktrees

    def create(self, path: str, branch: str, *, confirmed: bool | None = None) -> bool:
        result = self._repo.run(
            ["worktree", "add", path, branch],
            confirmed=confirmed,
            actor=self._actor,
        )
        return result.returncode == 0

    def lock(self, path: str, *, confirmed: bool | None = None) -> bool:
        result = self._repo.run(
            ["worktree", "lock", path],
            confirmed=confirmed,
            actor=self._actor,
        )
        return result.returncode == 0

    def unlock(self, path: str, *, confirmed: bool | None = None) -> bool:
        result = self._repo.run(
            ["worktree", "unlock", path],
            confirmed=confirmed,
            actor=self._actor,
        )
        return result.returncode == 0

    def prune(self, *, confirmed: bool | None = None) -> bool:
        result = self._repo.run(
            ["worktree", "prune"],
            confirmed=confirmed,
            actor=self._actor,
        )
        return result.returncode == 0

    def stale_worktrees(self) -> list[str]:
        result = self._repo.run(["worktree", "prune", "--dry-run", "-v"])
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    def branch_occupancy(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for wt in self.list_worktrees():
            branch = wt.get("branch", "")
            if not branch:
                continue
            mapping.setdefault(branch, []).append(wt["path"])
        return mapping
