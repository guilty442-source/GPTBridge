"""Git Fault Injection Framework — §218-220, §299-301, §306.

Test-only fault injection for the governed Git stack.  Every injector
operates exclusively on a ``TestRepoFixture`` produced by
``create_test_repository()`` — a temporary root carrying the
``gptbridge-test-repo`` marker.  Injecting into the real repository or
any origin is refused at construction.

Layout (§219)::

    TempRoot/
      main/            primary worktree (branch: main)
      worktrees/wNNN/  worker worktrees (branch: wNNN)
      audit/           audit store
      recovery/        recovery bundles
      registry/        worker registry
      logs/            test logs
      gptbridge-test-repo   safety marker
      manifest.json    seed / versions / test_run_id (§221)
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_TEST_MARKER = "gptbridge-test-repo"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fault Injection",
    "GIT_AUTHOR_EMAIL": "fault@test.local",
    "GIT_COMMITTER_NAME": "Fault Injection",
    "GIT_COMMITTER_EMAIL": "fault@test.local",
    # Fixed dates keep seeded fixtures bit-for-bit reproducible (§221).
    "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+00:00",
}


class ProductionRepoError(RuntimeError):
    """Raised when fault injection targets a non-test repository."""


class InvariantViolation(RuntimeError):
    """Raised by ``assert_git_invariants(fail_fast=True)``."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("invariant violations: " + "; ".join(violations))


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_GIT_ENV}
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def git_version() -> str:
    out = subprocess.run(
        ["git", "--version"], capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return (out.stdout or "").strip()


def canonical_path(path: str | Path) -> str:
    """§309 — case-insensitive canonical form for path comparison."""
    return os.path.normcase(os.path.normpath(str(Path(path).resolve())))


# ---------------------------------------------------------------------------
# Safety guard (§219, §306)
# ---------------------------------------------------------------------------


def is_test_repository(root: str | Path) -> bool:
    """True only for marker-bearing roots under a temp/test tree."""
    root_path = Path(root).resolve()
    marker = root_path / _TEST_MARKER
    git_marker = root_path / ".git" / _TEST_MARKER
    if not (marker.is_file() or git_marker.is_file()):
        return False
    temp_root = Path(tempfile.gettempdir()).resolve()
    canonical = canonical_path(root_path)
    if canonical.startswith(canonical_path(temp_root)):
        return True
    parts = {p.lower() for p in root_path.parts}
    return bool(parts & {"tmp", "temp", "test", "tests", "fixtures"})


def assert_test_repository(root: str | Path) -> Path:
    """§306 — refuse anything that is not a disposable test repository.

    Hard-blocks the real project root, its ``.git`` and any path equal to
    or containing it, regardless of markers.
    """
    root_path = Path(root).resolve()
    real = canonical_path(_PROJECT_ROOT)
    target = canonical_path(root_path)
    if target == real or target.startswith(real + os.sep) or \
            real.startswith(target + os.sep):
        raise ProductionRepoError(
            f"refusing fault injection on production path: {root_path}")
    if not is_test_repository(root_path):
        raise ProductionRepoError(
            f"missing {_TEST_MARKER} marker / non-test root: {root_path}")
    return root_path


# ---------------------------------------------------------------------------
# Repository factory (§220-221)
# ---------------------------------------------------------------------------


@dataclass
class TestRepoFixture:
    """A disposable governed repository tree."""
    root: Path
    test_run_id: str
    seed: int
    main: Path
    worktrees_dir: Path
    audit_dir: Path
    recovery_dir: Path
    registry_dir: Path
    logs_dir: Path
    workers: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: Path = Path()

    def worker(self, index: int) -> dict[str, Any]:
        return self.workers[index]

    def write_manifest(self, extra: Optional[dict[str, Any]] = None) -> None:
        manifest = {
            "test_run_id": self.test_run_id,
            "seed": self.seed,
            "git_version": git_version(),
            "os": platform.platform(),
            "python_version": platform.python_version(),
            "workers": self.workers,
            **(extra or {}),
        }
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")


def create_test_repository(
    base_dir: str | Path,
    *,
    seed: int = 0,
    test_run_id: str = "",
    commit_count: int = 1,
    branch_count: int = 0,
    worktree_count: int = 0,
    file_count: int = 1,
    conflict_pattern: Optional[str] = None,
    origin_mirror: bool = False,
    audit_enabled: bool = True,
    hooks_enabled: bool = True,
) -> TestRepoFixture:
    """Build a deterministic governed test repository (§220-221)."""
    rng = random.Random(seed)
    root = Path(base_dir).resolve()
    run_id = test_run_id or uuid.uuid4().hex[:12]
    root.mkdir(parents=True, exist_ok=True)

    main = root / "main"
    worktrees_dir = root / "worktrees"
    audit_dir = root / "audit"
    recovery_dir = root / "recovery"
    registry_dir = root / "registry"
    logs_dir = root / "logs"
    for d in (main, worktrees_dir, audit_dir, recovery_dir,
              registry_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Main repository.
    _git(root, "init", "-b", "main", str(main))
    _git(main, "config", "user.name", "Fault Injection")
    _git(main, "config", "user.email", "fault@test.local")
    for i in range(max(1, file_count)):
        (main / f"file-{i}.txt").write_text(
            f"seed={seed} file={i} v0\n" + "\n".join(
                f"line-{j}-{rng.randint(0, 10**6)}" for j in range(8)
            ) + "\n", encoding="utf-8")
    for c in range(max(1, commit_count)):
        (main / f"file-{c % max(1, file_count)}.txt").write_text(
            f"seed={seed} file={c % max(1, file_count)} v{c}\n"
            + "\n".join(f"line-{j}-{rng.randint(0, 10**6)}"
                        for j in range(8)) + "\n", encoding="utf-8")
        _git(main, "add", "-A")
        _git(main, "commit", "-m", f"seed-{seed} commit {c}")

    # Extra branches on main (non-worktree).
    for b in range(branch_count):
        _git(main, "branch", f"topic-{b}")

    # Optional origin mirror.
    if origin_mirror:
        origin = root / "origin.git"
        _git(root, "init", "--bare", str(origin))
        _git(main, "remote", "add", "origin", str(origin))
        _git(main, "push", "origin", "main")

    # Worker worktrees w001..wNNN, one unique branch each.
    workers: list[dict[str, Any]] = []
    for w in range(1, worktree_count + 1):
        name = f"w{w:03d}"
        branch = name
        path = worktrees_dir / name
        _git(main, "worktree", "add", str(path), "-b", branch, "main")
        workers.append({
            "worker_id": name, "branch": branch,
            "worktree": str(path), "watcher_pid": None,
            "commit_cursor": 0,
        })

    # Safety marker + manifest (§219, §221, §306).
    (root / _TEST_MARKER).write_text(run_id, encoding="utf-8")
    (main / ".git" / _TEST_MARKER).write_text(run_id, encoding="utf-8")

    fixture = TestRepoFixture(
        root=root, test_run_id=run_id, seed=seed, main=main,
        worktrees_dir=worktrees_dir,
        audit_dir=audit_dir, recovery_dir=recovery_dir,
        registry_dir=registry_dir, logs_dir=logs_dir,
        workers=workers,
        manifest_path=root / "manifest.json",
    )
    fixture.write_manifest()
    (registry_dir / "registry.json").write_text(
        json.dumps({"generation": 1, "workers": workers}, indent=2),
        encoding="utf-8")
    (registry_dir / "registry.previous").write_text(
        (registry_dir / "registry.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    (audit_dir / "audit.jsonl").write_text("", encoding="utf-8")
    return fixture


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


# ---------------------------------------------------------------------------
# Invariant checker (§299-300)
# ---------------------------------------------------------------------------


def _heads(repo: Path, ref: str) -> str:
    out = _git(repo, "rev-parse", "--verify", ref, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def assert_git_invariants(
    fixture: TestRepoFixture, *, fail_fast: bool = False,
) -> list[str]:
    """§299 — check the governed invariants against real Git state.

    Returns a list of violation strings (empty = all hold).  With
    ``fail_fast=True`` raises ``InvariantViolation`` so chaos runners
    stop before touching the fixture further (§300).
    """
    violations: list[str] = []
    main = fixture.main

    # main has no watcher / single writer: registry must not assign one.
    reg_path = fixture.registry_dir / "registry.json"
    try:
        registry = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        violations.append("registry-corrupt-or-missing")
        registry = {"workers": []}

    branches: dict[str, str] = {}
    worktrees: set[str] = set()
    for worker in fixture.workers:
        wt = Path(worker["worktree"])
        branch = worker["branch"]
        if branch in branches:
            violations.append(
                f"branch-collision:{branch}:{branches[branch]}/"
                f"{worker['worker_id']}")
        branches[branch] = worker["worker_id"]
        canonical = canonical_path(wt)
        if canonical in worktrees:
            violations.append(f"worktree-collision:{wt}")
        worktrees.add(canonical)

    # queue source SHA immutability: queue files pin a sha field.
    queue_dir = fixture.root / "queue"
    if queue_dir.is_dir():
        for entry in queue_dir.glob("*.json"):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                violations.append(f"queue-entry-corrupt:{entry.name}")
                continue
            if not data.get("source_sha"):
                violations.append(f"queue-entry-missing-sha:{entry.name}")

    # audit sequence monotonic + valid JSONL tail.
    audit_log = fixture.audit_dir / "audit.jsonl"
    if audit_log.is_file():
        last_seq = -1
        for lineno, line in enumerate(
                audit_log.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                violations.append(f"audit-partial-tail:line-{lineno}")
                break
            seq = int(rec.get("sequence", -1))
            if seq <= last_seq:
                violations.append(f"audit-sequence-nonmonotonic:{lineno}")
            last_seq = seq

    # no merge in progress left dangling on main.
    if (main / ".git" / "MERGE_HEAD").is_file():
        violations.append("main-merge-in-progress")

    if fail_fast and violations:
        raise InvariantViolation(violations)
    return violations


# ---------------------------------------------------------------------------
# Failure artifact bundle (§301)
# ---------------------------------------------------------------------------


def capture_failure_artifacts(
    fixture: TestRepoFixture, reason: str,
    violations: Optional[list[str]] = None,
) -> Path:
    """Preserve the failing fixture — never cleanup the scene (§300-301)."""
    out = fixture.root / "failure-artifacts" / fixture.test_run_id
    out.mkdir(parents=True, exist_ok=True)
    for name in ("audit", "registry", "logs"):
        src = getattr(fixture, f"{name}_dir", None) or fixture.root / name
        if Path(src).is_dir():
            shutil.copytree(src, out / name, dirs_exist_ok=True)
    refs = _git(fixture.main, "for-each-ref",
                "--format=%(refname) %(objectname)", check=False)
    (out / "refs.txt").write_text(refs.stdout or "", encoding="utf-8")
    (out / "worktrees.txt").write_text(
        json.dumps(fixture.workers, indent=2), encoding="utf-8")
    (out / "failure.json").write_text(json.dumps({
        "test_run_id": fixture.test_run_id, "seed": fixture.seed,
        "reason": reason, "violations": violations or [],
        "git_version": git_version(), "os": platform.platform(),
        "python_version": platform.python_version(),
    }, indent=2), encoding="utf-8")
    return out


__all__ = [
    "FaultInjectionManager", "InvariantViolation", "ProductionRepoError",
    "TestRepoFixture", "assert_git_invariants", "assert_test_repository",
    "canonical_path", "capture_failure_artifacts", "create_test_repository",
    "git_version", "is_test_repository",
]
