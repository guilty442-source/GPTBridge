"""Supervised, persistent automation for the parallel worktree pipeline.

The supervisor is the single long-running owner of the GPTBridge git
automation stack.  It:

  * spawns and supervises one self-commit watcher per registered worktree
    (auto-restart with exponential backoff, per-watcher log files);
  * periodically runs the conflict-safe workspace synchronizer against
    ``main`` so external worker branches are integrated and clean workers
    are fast-forwarded;
  * records its own lifecycle, children, and last sync results in a JSON
    registry under the shared git common directory so any worktree can
    report or stop it;
  * can register itself as a Windows Task Scheduler logon job for
    cross-reboot persistence.

Governance (A53/E39, A58/E44): the supervisor never pushes unless ``--push``
is given (the synchronizer itself guards remote-sync durability), never
force-updates, never resolves conflicts automatically, and never deletes
refs.  All git writes flow through ``GitRepository.run`` with
``confirmed=True`` and are recorded in the tier audit ledger.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

from .git_repository import GitRepository
from .process_lock import LockBusyError, ProcessFileLock, pid_alive
from .worktree_manager import WorktreeManager
from .workspace_sync import synchronize

SUPERVISOR_ACTOR: Final[str] = "governance/automation-supervisor"
SUPERVISOR_STATE_SUBDIR: Final[str] = "gptbridge-automation"
REGISTRY_FILE: Final[str] = "registry.json"
SUPERVISOR_LOCK: Final[str] = "supervisor.lock"

_logger = logging.getLogger("gptbridge.automation_supervisor")


def state_dir(root: str | Path) -> Path:
    """Shared automation-state directory under the git common dir."""
    repo = GitRepository(root)
    common_result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (common_result.stdout or "").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo.path / common
    state = common.resolve() / SUPERVISOR_STATE_SUBDIR
    state.mkdir(parents=True, exist_ok=True)
    return state


def _branch_of(root: str | Path, worktree_path: str) -> str:
    repo = GitRepository(worktree_path)
    return repo.current_branch() or "HEAD"


def _setup_logging(root: str | Path, directory: Path, name: str) -> logging.Logger:
    logs = directory / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(logs / f"{name}.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    return logger


class _Watcher:
    """One supervised self-commit watcher subprocess."""

    def __init__(
        self,
        worktree: str,
        branch: str,
        process: subprocess.Popen[str],
        log_path: Path,
    ) -> None:
        self.worktree = worktree
        self.branch = branch
        self.process = process
        self.log_path = log_path
        self.started_at = time.time()
        self.restarts = 0
        self.dead_since: float | None = None

    def alive(self) -> bool:
        return self.process.poll() is None

    def restart_delay(self) -> float:
        return min(300.0, 5.0 * (2 ** self.restarts))

    def to_dict(self) -> dict[str, object]:
        return {
            "worktree": self.worktree,
            "branch": self.branch,
            "pid": self.process.pid,
            "log": str(self.log_path),
            "started_at": self.started_at,
            "restarts": self.restarts,
        }


def _spawn_watcher(
    root: str | Path,
    worktree: str,
    branch: str,
    directory: Path,
    interval: float,
    debounce: float,
) -> _Watcher:
    logs = directory / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in branch)
    log_path = logs / f"watcher-{safe}.log"
    log_file = log_path.open("a", encoding="utf-8")
    module = "governance_rule.execution.git_tiers.self_commit"
    cmd = [
        sys.executable,
        "-m",
        module,
        "--worktree",
        worktree,
        "--watch",
        "--interval",
        str(int(interval)),
        "--debounce",
        str(int(debounce)),
        "--actor",
        SUPERVISOR_ACTOR,
    ]
    process = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=log_file,
        stderr=log_file,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return _Watcher(worktree, branch, process, log_path)


def read_registry(root: str | Path) -> dict[str, object] | None:
    """Read the supervisor registry, or ``None`` when the service is absent."""
    registry_path = state_dir(root) / REGISTRY_FILE
    if not registry_path.is_file():
        return None
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_registry(directory: Path, payload: dict[str, object]) -> None:
    registry_path = directory / REGISTRY_FILE
    tmp = registry_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, registry_path)


def supervise(
    root: str | Path,
    *,
    sync_interval: float = 60.0,
    health_interval: float = 20.0,
    watch_interval: float = 30.0,
    debounce: float = 60.0,
    commit_dirty: bool = False,
    push: bool = False,
) -> None:
    """Supervise worktree watchers and run periodic synchronization.

    Blocks until the process receives a stop (``--stop`` removes the lock and
    the loop exits once the lock is released).  When another supervisor
    already owns the lock this returns immediately instead of crashing.
    """
    directory = state_dir(root)
    logger = _setup_logging(root, directory, "supervisor")
    lock_path = directory / SUPERVISOR_LOCK
    lock = ProcessFileLock(lock_path)

    try:
        with lock:
            supervise_loop(root, logger, lock_path, directory,
                           sync_interval=sync_interval,
                           health_interval=health_interval,
                           watch_interval=watch_interval,
                           debounce=debounce,
                           commit_dirty=commit_dirty, push=push)
    except LockBusyError:
        logger.info("another supervisor owns the lock; exiting")
        return


def supervise_loop(
    root: str | Path,
    logger: logging.Logger,
    lock_path: Path,
    directory: Path,
    *,
    sync_interval: float,
    health_interval: float,
    watch_interval: float,
    debounce: float,
    commit_dirty: bool,
    push: bool,
) -> None:
    """Run the supervision loop.  The caller owns ``lock_path``."""
    monitor = GitRepository(root)
    manager = WorktreeManager(monitor)
    registry: dict[str, object] = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "sync_interval": sync_interval,
        "health_interval": health_interval,
        "commit_dirty": commit_dirty,
        "push": push,
        "last_sync": "",
        "last_sync_result": "",
        "sync_cycles": 0,
        "children": [],
    }
    watchers: dict[str, _Watcher] = {}
    last_sync_at = time.time()
    logger.info("supervisor started pid=%d root=%s", os.getpid(), root)

    while True:
        try:
            worktrees = manager.list_worktrees()
            known = {Path(item["path"]).resolve() for item in worktrees}

            for watcher_path in list(watchers):
                if Path(watcher_path).resolve() not in known:
                    watchers.pop(watcher_path)

            for item in worktrees:
                resolved = str(Path(item["path"]).resolve())
                existing = watchers.get(resolved)
                if existing is not None:
                    if existing.alive():
                        continue
                    if existing.dead_since is None:
                        existing.dead_since = time.time()
                    if (time.time() - existing.dead_since) < existing.restart_delay():
                        continue
                    logger.warning("restarting watcher pid=%d %s",
                                   existing.process.pid, resolved)
                    _terminate_tree(existing.process.pid)
                    watcher = _spawn_watcher(
                        root, resolved, _branch_of(root, resolved),
                        directory, watch_interval, debounce,
                    )
                    watcher.restarts = existing.restarts + 1
                    watchers[resolved] = watcher
                    continue
                branch = _branch_of(root, resolved)
                watcher = _spawn_watcher(
                    root, resolved, branch, directory,
                    watch_interval, debounce,
                )
                watchers[resolved] = watcher
                logger.info("spawned watcher pid=%d %s (%s)",
                            watcher.process.pid, resolved, branch)

            if time.time() - last_sync_at >= sync_interval:
                last_sync_at = time.time()
                registry["sync_cycles"] = int(registry.get("sync_cycles", 0)) + 1
                try:
                    result = synchronize(
                        root, commit_dirty=commit_dirty, push=push
                    )
                except Exception as exc:  # keep the loop alive
                    result = f"error:{type(exc).__name__}"
                registry["last_sync"] = time.time()
                registry["last_sync_result"] = result
                logger.info("sync cycle %s -> %s",
                            registry["sync_cycles"], result)

            registry["children"] = [w.to_dict() for w in watchers.values()]
            _write_registry(directory, registry)

            if not lock_path.exists() or not lock_is_active_here(lock_path):
                logger.info("stop requested; shutting down")
                break
        except Exception as exc:  # never let the supervisor die silently
            logger.error("supervisor loop error: %s", type(exc).__name__)
        time.sleep(health_interval)

    for watcher in watchers.values():
        _terminate_tree(watcher.process.pid)
    logger.info("supervisor stopped")


def _terminate_tree(pid: int) -> None:
    """Terminate ``pid`` and all its descendants (OS-portable, psutil)."""
    try:
        import psutil
    except ImportError:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        return
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass


def lock_is_active_here(path: Path) -> bool:
    """True while this process still owns the registry lock file."""
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
        return int(payload.get("pid", -1)) == os.getpid()
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def start_background(
    root: str | Path,
    *,
    sync_interval: float,
    health_interval: float,
    watch_interval: float,
    debounce: float,
    commit_dirty: bool,
    push: bool,
) -> int:
    """Launch the supervisor as a detached background process (no console)."""
    module = "governance_rule.execution.git_tiers.automation_supervisor"
    cmd = [
        sys.executable,
        "-m",
        module,
        "--root",
        str(root),
        "--foreground",
        "--sync-interval",
        str(int(sync_interval)),
        "--health-interval",
        str(int(health_interval)),
        "--watch-interval",
        str(int(watch_interval)),
        "--debounce",
        str(int(debounce)),
    ]
    if commit_dirty:
        cmd.append("--commit-dirty")
    if push:
        cmd.append("--push")
    handle = subprocess.Popen(  # noqa: S603
        cmd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return handle.pid


def _unlink_with_retry(path: Path, tries: int = 5, delay: float = 0.3) -> None:
    """Delete ``path``, retrying briefly while Windows releases handles."""
    for attempt in range(tries):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(delay)


def stop(root: str | Path) -> str:
    """Stop the supervisor by removing its lock and terminating children."""
    directory = state_dir(root)
    lock_path = directory / SUPERVISOR_LOCK
    registry = read_registry(root)
    supervisor_pid = int(registry.get("pid", 0)) if registry else 0
    terminated: list[int] = []
    if registry:
        for child in registry.get("children", []):
            pid = int(child.get("pid", 0))
            if pid and pid_alive(pid):
                _terminate_tree(pid)
                terminated.append(pid)
    for pid in (supervisor_pid,):
        if pid and pid_alive(pid):
            _terminate_tree(pid)
            terminated.append(pid)
    _unlink_with_retry(lock_path)
    _unlink_with_retry(directory / REGISTRY_FILE)
    detail = f"terminated={terminated}" if terminated else "no live processes"
    return f"stopped ({detail})"


def status(root: str | Path) -> dict[str, object]:
    """Return a status summary for the supervised automation service."""
    directory = state_dir(root)
    registry = read_registry(root)
    lock_path = directory / SUPERVISOR_LOCK
    running = lock_path.is_file()
    summary: dict[str, object] = {
        "running": running,
        "registry_exists": registry is not None,
        "state_dir": str(directory),
    }
    if registry:
        summary["supervisor_pid"] = registry.get("pid")
        summary["started_at"] = registry.get("started_at")
        summary["sync_cycles"] = registry.get("sync_cycles", 0)
        summary["last_sync_result"] = registry.get("last_sync_result", "")
        summary["push"] = registry.get("push", False)
        summary["children"] = registry.get("children", [])
    return summary


def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry point for the automation supervisor service."""
    parser = argparse.ArgumentParser(
        description="Supervised persistent automation for parallel worktree sync."
    )
    parser.add_argument("--root", default=os.getcwd(), help="repository root")
    parser.add_argument("--start", action="store_true", help="start as a detached background service")
    parser.add_argument("--foreground", action="store_true", help="run the supervision loop in this process")
    parser.add_argument("--stop", action="store_true", help="stop a running supervisor")
    parser.add_argument("--status", action="store_true", help="report service status")
    parser.add_argument("--install-task", action="store_true", help="register a Task Scheduler logon job (needs elevation)")
    parser.add_argument("--uninstall-task", action="store_true", help="remove the Task Scheduler job")
    parser.add_argument("--install-logon", action="store_true", help="register a per-user logon Run key (no elevation)")
    parser.add_argument("--uninstall-logon", action="store_true", help="remove the per-user logon Run key")
    parser.add_argument("--sync-interval", type=float, default=60.0, help="sync cycle seconds")
    parser.add_argument("--health-interval", type=float, default=20.0, help="health poll seconds")
    parser.add_argument("--watch-interval", type=float, default=30.0, help="watch poll seconds")
    parser.add_argument("--debounce", type=float, default=60.0, help="commit stability seconds")
    parser.add_argument("--commit-dirty", action="store_true",
                        help="let the sync coordinator commit (use when no watchers)")
    parser.add_argument("--push", action="store_true",
                        help="allow the coordinator to push main after integration")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if args.status:
        print(json.dumps(status(root), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.stop:
        print(stop(root))
        return 0
    if args.install_task:
        return _install_task(root)
    if args.uninstall_task:
        return _uninstall_task()
    if args.install_logon:
        return _install_logon(root)
    if args.uninstall_logon:
        return _uninstall_logon()

    if args.foreground:
        supervise(
            root,
            sync_interval=args.sync_interval,
            health_interval=args.health_interval,
            watch_interval=args.watch_interval,
            debounce=args.debounce,
            commit_dirty=args.commit_dirty,
            push=args.push,
        )
        return 0
    if args.start:
        pid = start_background(
            root,
            sync_interval=args.sync_interval,
            health_interval=args.health_interval,
            watch_interval=args.watch_interval,
            debounce=args.debounce,
            commit_dirty=args.commit_dirty,
            push=args.push,
        )
        print(f"supervisor started (pid {pid})")
        return 0

    parser.error("one of --start/--foreground/--stop/--status is required")
    return 2


TASK_NAME = "GPTBridge-GitAutomation"


def _supervisor_launch_arguments(root: Path) -> str:
    """CWD-independent supervisor launch (wrapper script, not ``-m``).

    Task Scheduler and the logon ``Run`` key start processes with an
    unrelated working directory, so ``-m governance_rule...`` cannot
    resolve its package; the wrapper script inserts the project root into
    ``sys.path`` itself.
    """
    wrapper = root / "scripts" / "git-supervisor.py"
    return (
        f'"{wrapper}" --root "{root}" --foreground --sync-interval 60 '
        f'--health-interval 20 --watch-interval 30 --debounce 60'
    )


def _install_task(root: Path) -> int:
    """Register a logon Task Scheduler job that keeps the supervisor alive."""
    import tempfile

    target = (
        root / "main-system" / ".venv" / "Scripts" / "pythonw.exe"
        if (root / "main-system" / ".venv" / "Scripts" / "pythonw.exe").is_file()
        else sys.executable
    )
    arguments = _supervisor_launch_arguments(root)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="InteractiveUser">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="InteractiveUser">
    <Exec>
      <Command>{target}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", delete=False, encoding="utf-16"
    ) as handle:
        handle.write(xml)
        task_xml = handle.name
    try:
        result = subprocess.run(  # noqa: S603
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", task_xml, "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        try:
            os.unlink(task_xml)
        except OSError:
            pass
    if result.returncode != 0:
        print(f"task registration failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"task registered: {TASK_NAME}")
    return 0


def _uninstall_task() -> int:
    result = subprocess.run(  # noqa: S603
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 and "does not exist" not in result.stderr:
        print(f"task removal failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"task removed: {TASK_NAME}")
    return 0


LOGO_NAME = "GPTBridge-GitAutomation"
LOGO_HIVE = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _installed_pythonw(root: Path) -> str:
    target = root / "main-system" / ".venv" / "Scripts" / "pythonw.exe"
    if target.is_file():
        return str(target)
    return str(sys.executable)


def _install_logon(root: Path) -> int:
    """Register a per-user logon ``Run`` value for cross-reboot persistence."""
    import winreg

    pythonw = _installed_pythonw(root)
    arguments = _supervisor_launch_arguments(root)
    value = f'"{pythonw}" {arguments}'
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, LOGO_HIVE, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, LOGO_NAME, 0, winreg.REG_SZ, value)
    except OSError as exc:
        print(f"logon registration failed: {exc}", file=sys.stderr)
        return 1
    print(f"logon registration active: {LOGO_NAME}")
    return 0


def _uninstall_logon() -> int:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, LOGO_HIVE, 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                winreg.DeleteValue(key, LOGO_NAME)
            except FileNotFoundError:
                pass
    except OSError as exc:
        print(f"logon removal failed: {exc}", file=sys.stderr)
        return 1
    print(f"logon registration removed: {LOGO_NAME}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    raise SystemExit(cli_main())