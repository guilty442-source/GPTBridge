"""Thin git driver used by worktree manager and audit snapshot."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final


class GitRepository:
    """Wrapper around a git worktree or bare repository."""

    def __init__(self, path: str | Path = Path.cwd()) -> None:
        self.path: Final[Path] = Path(path).resolve()

    def run(self, args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def head(self) -> str:
        return self.run(["rev-parse", "HEAD"]).stdout.strip()

    def current_branch(self) -> str:
        return self.run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() or "HEAD"

    def is_bare(self) -> bool:
        return self.run(["rev-parse", "--is-bare-repository"]).stdout.strip() == "true"

    def status(self) -> str:
        return self.run(["status", "--short"]).stdout.strip()
