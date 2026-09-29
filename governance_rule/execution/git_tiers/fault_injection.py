"""Git Fault Injection Framework — §218-220, §299-301, §306.

Test-only fault injection for the governed Git stack.  Every injector
operates exclusively on a ``TestRepoFixture`` produced by
``create_test_repository()`` — a temporary root carrying the
``gptbridge-test-repo`` marker.  Injecting into the real repository or
any origin is refused at construction.

Module layout (A185 source-size split):

    fault_injection_repo.py     marker/env, §306 guard, §220 fixture factory
    fault_injection_checks.py   §299 invariant checker + §301 artifacts
    fault_injection.py          FaultInjectionManager injectors (this module)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .fault_injection_checks import (  # noqa: F401  (re-exported check surface)
    assert_git_invariants,
    capture_failure_artifacts,
    _heads,
)
from .fault_injection_repo import (  # noqa: F401  (re-exported repo surface)
    InvariantViolation,
    ProductionRepoError,
    TestRepoFixture,
    _git,
    assert_test_repository,
    canonical_path,
    create_test_repository,
    git_version,
    is_test_repository,
)


# ---------------------------------------------------------------------------
# FaultInjectionManager (§218)
# ---------------------------------------------------------------------------


class FaultInjectionManager:
    """Fault injectors bound to one disposable fixture.

    All methods operate inside ``fixture.root`` only; construction
    enforces §306 (test-marker + non-production path).
    """

    def __init__(self, fixture: TestRepoFixture) -> None:
        self.fixture = fixture
        assert_test_repository(fixture.root)
        self.active_faults: list[dict[str, Any]] = []

    def _record(self, kind: str, detail: dict[str, Any]) -> dict[str, Any]:
        fault = {
            "fault_id": uuid.uuid4().hex[:12], "kind": kind,
            "test_run_id": self.fixture.test_run_id,
            "at": time.time(), **detail,
        }
        self.active_faults.append(fault)
        return fault

    def inject_process_crash(self, pid: int) -> dict[str, Any]:
        """Terminate a process tree (watcher/coordinator simulation)."""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.kill(pid, 9)
        except OSError:
            pass
        return self._record("process_crash", {"pid": pid})

    def inject_timeout(self, command: str, delay_ms: int) -> dict[str, Any]:
        """Install a git shim that sleeps before ``command``.

        Returns a PATH override; callers run git with
        ``env PATH=<shim>:$PATH`` to exercise timeout handling.
        """
        shim_dir = self.fixture.root / "shims"
        shim_dir.mkdir(exist_ok=True)
        real_git = shutil.which("git") or "git"
        if sys.platform == "win32":
            shim = shim_dir / "git.bat"
            shim.write_text(
                f'@echo off\nif "%~1"=="{command}" '
                f'(powershell -c "Start-Sleep -ms {delay_ms}")\n'
                f'"{real_git}" %*\n', encoding="utf-8")
        else:
            shim = shim_dir / "git"
            shim.write_text(
                f'#!/bin/sh\nif [ "$1" = "{command}" ]; '
                f'then sleep {delay_ms / 1000}; fi\n'
                f'exec "{real_git}" "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        return self._record("timeout", {
            "command": command, "delay_ms": delay_ms,
            "shim_dir": str(shim_dir)})

    def inject_lock_contention(self, name: str = "merge.lock") -> dict[str, Any]:
        lock = self.fixture.registry_dir / name
        lock.write_text(str(os.getpid()), encoding="utf-8")
        return self._record("lock_contention", {"lock": str(lock)})

    def inject_audit_write_failure(self) -> dict[str, Any]:
        """Make the audit store unwritable (read-only dir)."""
        audit = self.fixture.audit_dir
        for entry in audit.iterdir():
            try:
                entry.chmod(0o444)
            except OSError:
                pass
        audit.chmod(0o555)
        return self._record("audit_write_failure",
                            {"audit_dir": str(audit)})

    def restore_audit_writes(self) -> None:
        audit = self.fixture.audit_dir
        try:
            audit.chmod(0o755)
        except OSError:
            pass
        for entry in audit.iterdir():
            try:
                entry.chmod(0o644)
            except OSError:
                pass

    def inject_hook_failure(
        self, hook: str = "pre-commit", mode: str = "missing",
    ) -> dict[str, Any]:
        """Corrupt/remove a client hook (missing|syntax|hash|unexec)."""
        hooks_dir = self.fixture.main / ".git" / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        target = hooks_dir / hook
        if mode == "missing":
            if target.exists():
                target.unlink()
        elif mode == "syntax":
            target.write_text("exit 1  # injected failure\n",
                              encoding="utf-8")
        elif mode == "unexec":
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            try:
                target.chmod(0o444)
            except OSError:
                pass
        else:  # hash mismatch — replace with different content
            target.write_text("# tampered hook\nexit 0\n",
                              encoding="utf-8")
        return self._record("hook_failure", {"hook": hook, "mode": mode})

    def inject_origin_unavailable(self) -> dict[str, Any]:
        _git(self.fixture.main, "remote", "set-url", "origin",
             str(self.fixture.root / "nonexistent-origin.git"),
             check=False)
        return self._record("origin_unavailable", {})

    def inject_merge_conflict(
        self, worker_a: int = 0, worker_b: int = 1,
        file: str = "file-0.txt", line: int = 3,
    ) -> dict[str, Any]:
        """Two workers modify the same line → guaranteed conflict."""
        fa, fb = (self.fixture.worker(worker_a),
                  self.fixture.worker(worker_b))
        for worker, tag in ((fa, "A"), (fb, "B")):
            path = Path(worker["worktree"]) / file
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[line] = f"conflict-{tag}-{self.fixture.seed}"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            _git(Path(worker["worktree"]), "add", "-A")
            _git(Path(worker["worktree"]), "commit", "-m",
                 f"{worker['worker_id']} conflict {tag}")
        return self._record("merge_conflict", {
            "workers": [fa["worker_id"], fb["worker_id"]],
            "file": file, "line": line})

    def inject_registry_corruption(self) -> dict[str, Any]:
        reg = self.fixture.registry_dir / "registry.json"
        # Keep .previous intact — fallback must survive.
        reg.write_text("{corrupted not json", encoding="utf-8")
        return self._record("registry_corruption", {"path": str(reg)})

    def inject_partial_audit_tail(self) -> dict[str, Any]:
        """Append a truncated JSONL record (crash mid-write)."""
        audit = self.fixture.audit_dir / "audit.jsonl"
        with audit.open("a", encoding="utf-8") as fh:
            fh.write('{"sequence": 999, "partial": tru')
        return self._record("partial_audit_tail", {"path": str(audit)})

    def inject_worktree_missing(self, worker_index: int = 0) -> dict[str, Any]:
        worker = self.fixture.worker(worker_index)
        path = Path(worker["worktree"])
        if path.exists():
            shutil.rmtree(path)
        return self._record("worktree_missing", {
            "worker_id": worker["worker_id"], "path": str(path)})

    def inject_index_lock(self, worker_index: Optional[int] = None
                          ) -> dict[str, Any]:
        target = (self.fixture.main if worker_index is None
                  else Path(self.fixture.worker(worker_index)["worktree"]))
        lock = target / ".git" / "index.lock"
        lock.write_text(str(os.getpid()), encoding="utf-8")
        return self._record("index_lock", {"lock": str(lock)})

    def inject_ref_drift(
        self, ref: str = "refs/heads/main", worker_index: int = 0,
    ) -> dict[str, Any]:
        """Move a branch ref the queue pinned (§230, §277)."""
        worker = self.fixture.worker(worker_index)
        _git(Path(worker["worktree"]), "commit", "--allow-empty",
             "-m", "drift commit")
        new_sha = _git(Path(worker["worktree"]), "rev-parse",
                       "HEAD").stdout.strip()
        return self._record("ref_drift", {
            "ref": worker["branch"], "new_sha": new_sha})

    def inject_disk_pressure_signal(self) -> dict[str, Any]:
        """Injectable disk monitor signal — never fills a real disk."""
        signal = self.fixture.root / "disk-pressure.signal"
        signal.write_text("LOW_SPACE", encoding="utf-8")
        return self._record("disk_pressure", {"signal": str(signal)})

    def repair_transient_faults(self) -> None:
        """Undo faults that are detection-only so a chaos run can
        continue after the invariant checker observed them."""
        # Partial audit tail: truncate back to the last complete line.
        audit = self.fixture.audit_dir / "audit.jsonl"
        if audit.is_file():
            lines = audit.read_text(encoding="utf-8").splitlines()
            good = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                    good.append(line)
                except json.JSONDecodeError:
                    break
            audit.write_text(
                "\n".join(good) + ("\n" if good else ""), encoding="utf-8")
        # Injected lock files + pressure signal.
        for lock in self.fixture.registry_dir.glob("*.lock"):
            try:
                lock.unlink()
            except OSError:
                pass
        signal = self.fixture.root / "disk-pressure.signal"
        if signal.exists():
            signal.unlink()

    def inject_gitfile_break(self, worker_index: int = 0) -> dict[str, Any]:
        """§261 — corrupt the worktree .git link file."""
        worker = self.fixture.worker(worker_index)
        gitfile = Path(worker["worktree"]) / ".git"
        # Windows AV/indexer can transiently lock the file — retry, then
        # fall back to replace-via-rename.
        for _ in range(5):
            try:
                gitfile.write_text("gitdir: /nonexistent/broken\n",
                                   encoding="utf-8")
                break
            except PermissionError:
                time.sleep(0.2)
        else:
            moved = gitfile.with_name(".git.orig")
            gitfile.rename(moved)
            gitfile.write_text("gitdir: /nonexistent/broken\n",
                               encoding="utf-8")
        return self._record("broken_gitfile", {"path": str(gitfile)})

    def inject_object_corruption(self) -> dict[str, Any]:
        """§263 — corrupt one loose object in the disposable repo."""
        objects = self.fixture.main / ".git" / "objects"
        loose = [p for d in objects.iterdir()
                 if d.is_dir() and len(d.name) == 2
                 for p in d.iterdir()]
        if loose:
            target = loose[0]
            try:
                target.chmod(0o666)  # objects are read-only
            except OSError:
                pass
            target.write_bytes(b"corrupted")
        return self._record("object_corruption", {})

    def inject_index_corruption(self, worker_index: int = 0
                                ) -> dict[str, Any]:
        worker = self.fixture.worker(worker_index)
        index = Path(worker["worktree"]) / ".git" / "index"
        # .git is a file in worktrees — resolve the real index.
        if gitfile := self._worktree_gitdir(Path(worker["worktree"])):
            index = gitfile / "index"
        if index.exists():
            index.write_bytes(b"corrupted-index")
        return self._record("index_corruption", {"index": str(index)})

    @staticmethod
    def _worktree_gitdir(worktree: Path) -> Optional[Path]:
        gitfile = worktree / ".git"
        if gitfile.is_file():
            content = gitfile.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                return Path(content.split(":", 1)[1].strip())
        return gitfile if gitfile.is_dir() else None


__all__ = [
    "FaultInjectionManager", "InvariantViolation", "ProductionRepoError",
    "TestRepoFixture", "assert_git_invariants", "assert_test_repository",
    "canonical_path", "capture_failure_artifacts", "create_test_repository",
    "git_version", "is_test_repository",
]
