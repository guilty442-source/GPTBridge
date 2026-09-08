from __future__ import annotations

import subprocess
import os
from pathlib import Path
from typing import Any

from governance_rule.execution.git_tiers import audit_log, classify, enforce
from governance_rule.execution.git_tiers.snapshot import _capture_repo_snapshot


class LocalGitRepository:
    """Loopback-equivalent Git adapter for system version and development history.

    All write operations (stage/commit) are read-only by default and must be
    explicitly authorized via ``confirmed=True`` (the governance approval gate).
    """

    _STAGING_GOVERNANCE_REQUIRED = (
        "git 本地寫入操作（stage/commit）需經治理審批。"
        "請明確確認要加入暫存與提交的檔案與範圍後再執行。"
    )

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()

    def _run(
        self,
        *arguments: str,
        check: bool = True,
        confirmed: bool | None = None,
        authority_approved: bool | None = None,
    ) -> str:
        command = " ".join(arguments)
        snapshot = _capture_repo_snapshot(self.project_root)
        allowed, message = enforce(
            command,
            "governance/tool/xingcheng",
            confirmed=confirmed,
            authority_approved=authority_approved,
            repo_snapshot=snapshot,
        )
        if not allowed:
            raise PermissionError(message)
        result = subprocess.run(
            ["git", "-C", str(self.project_root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        audit_log(
            classify(command),
            command,
            "governance/tool/xingcheng",
            True,
            result.stderr.strip()[:500],
            repo_snapshot=snapshot,
            phase="result",
            result="succeeded" if result.returncode == 0 else "failed",
            returncode=result.returncode,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                "GIT_LOCAL_COMMAND_FAILED: " + result.stderr.strip()[:500]
            )
        return result.stdout.rstrip()

    @staticmethod
    def _govern(confirmed: bool, *, message: str) -> None:
        if not confirmed:
            raise PermissionError("GIT_GOVERNANCE_APPROVAL_REQUIRED: " + message)

    def status(self) -> dict[str, Any]:
        root = Path(self._run("rev-parse", "--show-toplevel")).resolve()
        if root != self.project_root:
            raise RuntimeError("GIT_REPOSITORY_ROOT_MISMATCH")
        changes = [line for line in self._run("status", "--short").splitlines() if line]
        return {
            "available": True,
            "engine": "git",
            "role": "system-version-and-development-history",
            "repository": str(root),
            "branch": self._run("branch", "--show-current") or "detached",
            "revision": self._run("rev-parse", "HEAD"),
            "dirty": bool(changes),
            "change_count": len(changes),
            "changes": changes,
            "remote_contacted": False,
            "write_requires_governance_approval": True,
        }

    def diff_stat(self, *, confirmed: bool = False) -> dict[str, Any]:
        lines = [
            line
            for line in self._run(
                "diff", "--stat", "--no-color", "--", "HEAD", check=False
            ).splitlines()
            if line.strip()
        ]
        return {
            "ok": True,
            "instrumentation": "governed-local-read",
            "approved": True,
            "summary": lines,
            "write_requires_governance_approval": False,
        }

    def stage(self, paths: Any, *, confirmed: bool = False) -> dict[str, Any]:
        self._govern(
            confirmed,
            message=self._STAGING_GOVERNANCE_REQUIRED,
        )
        targets = list(dict.fromkeys(str(p).strip() for p in (paths or [])))
        if not targets:
            targets = ["."]
        self._run("add", "--", *targets, confirmed=confirmed)
        staged = [
            line
            for line in self._run("diff", "--cached", "--name-only").splitlines()
            if line.strip()
        ]
        return {
            "ok": True,
            "instrumentation": "governed-local-write",
            "approved": True,
            "operation": "stage",
            "staged_paths": staged,
            "staged_count": len(staged),
            "write_requires_governance_approval": True,
        }

    def commit(
        self,
        message: str,
        *,
        confirmed: bool = False,
        amend: bool = False,
        authority_approved: bool = False,
    ) -> dict[str, Any]:
        self._govern(
            confirmed,
            message=self._STAGING_GOVERNANCE_REQUIRED,
        )
        subject = str(message or "").strip()
        if not subject:
            raise RuntimeError("GIT_COMMIT_MESSAGE_REQUIRED")
        arguments = ["commit"]
        if amend:
            arguments.append("--amend")
        arguments += ["-m", subject]
        self._run(
            *arguments,
            confirmed=confirmed,
            authority_approved=authority_approved,
        )
        return {
            "ok": True,
            "instrumentation": "governed-local-write",
            "approved": True,
            "operation": "amend" if amend else "commit",
            "revision": self._run("rev-parse", "HEAD"),
            "subject": subject,
            "write_requires_governance_approval": True,
        }

    def history(self, *, limit: int = 20) -> dict[str, Any]:
        count = max(1, min(100, int(limit)))
        separator = "\x1f"
        records = []
        output = self._run(
            "log", f"-{count}", "--date=iso-strict",
            f"--pretty=format:%H{separator}%ad{separator}%an{separator}%s",
        )
        for line in output.splitlines():
            fields = line.split(separator, 3)
            if len(fields) == 4:
                records.append(dict(zip(("revision", "timestamp", "author", "subject"), fields)))
        return {"ok": True, "commits": records, "count": len(records), "remote_contacted": False}


__all__ = ["LocalGitRepository"]
