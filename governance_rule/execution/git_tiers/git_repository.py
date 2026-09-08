"""Thin git driver used by worktree manager and audit snapshot."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

from . import audit_log, classify, enforce
from .snapshot import _capture_repo_snapshot


class GitRepository:
    """Wrapper around a git worktree or bare repository."""

    def __init__(self, path: str | Path = Path.cwd()) -> None:
        self.path: Final[Path] = Path(path).resolve()

    def run(
        self,
        args: list[str],
        *,
        check: bool = False,
        confirmed: bool | None = None,
        authority_approved: bool | None = None,
        actor: str = "governance/git-repository",
    ) -> subprocess.CompletedProcess[str]:
        command = " ".join(str(item) for item in args)
        snapshot = _capture_repo_snapshot(self.path)
        allowed, message = enforce(
            command,
            actor,
            confirmed=confirmed,
            authority_approved=authority_approved,
            repo_snapshot=snapshot,
        )
        if not allowed:
            raise PermissionError(message)
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        audit_log(
            classify(command),
            command,
            actor,
            True,
            result.stderr.strip()[:500],
            repo_snapshot=snapshot,
            phase="result",
            result="succeeded" if result.returncode == 0 else "failed",
            returncode=result.returncode,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result

    def head(self) -> str:
        return self.run(["rev-parse", "HEAD"]).stdout.strip()

    def current_branch(self) -> str:
        return self.run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() or "HEAD"

    def is_bare(self) -> bool:
        return self.run(["rev-parse", "--is-bare-repository"]).stdout.strip() == "true"

    def status(self) -> str:
        return self.run(["status", "--short"]).stdout.strip()
