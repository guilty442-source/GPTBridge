"""Supervisor state directory, registry, logging, and stop/status helpers.

Owns the shared ``.git/gptbridge-automation`` state directory layout and the
registry file so the facade and the supervision loop stay small (A430/E160).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Final

from .git_repository import GitRepository
from .process_lock import pid_alive

SUPERVISOR_ACTOR: Final[str] = "governance/automation-supervisor"
SUPERVISOR_STATE_SUBDIR: Final[str] = "gptbridge-automation"
REGISTRY_FILE: Final[str] = "registry.json"
SUPERVISOR_LOCK: Final[str] = "supervisor.lock"


def _state_dir(root: str | Path) -> Path:
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


def _read_registry(root: str | Path) -> dict[str, object] | None:
    """Read the supervisor registry, or ``None`` when the service is absent."""
    registry_path = _state_dir(root) / REGISTRY_FILE
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


def _terminate_tree(pid: int) -> None:
    """Terminate ``pid`` and all its descendants (native process metrics)."""
    try:
        from shared_layer.performance import process_metrics
    except ImportError:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        return
    if not process_metrics.process_alive(pid):
        return
    for child_pid in process_metrics.process_children(pid):
        process_metrics.process_terminate(child_pid)
    process_metrics.process_terminate(pid)


def _lock_is_active_here(path: Path) -> bool:
    """True while this process still owns the registry lock file."""
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
        return int(payload.get("pid", -1)) == os.getpid()
    except (OSError, ValueError, json.JSONDecodeError):
        return False


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
    directory = _state_dir(root)
    lock_path = directory / SUPERVISOR_LOCK
    registry = _read_registry(root)
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
    directory = _state_dir(root)
    registry = _read_registry(root)
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
        summary["state"] = registry.get("state", "RUNNING" if running else "STOPPED")
        summary["sync_cycles"] = registry.get("sync_cycles", 0)
        summary["last_sync_result"] = registry.get("last_sync_result", "")
        summary["push"] = registry.get("push", False)
        summary["children"] = registry.get("children", [])
        if isinstance(registry.get("health"), dict):
            summary["health"] = registry["health"]
    if "health" not in summary:
        try:
            from .automation_supervisor_loop import _health_surfaces

            summary["health"] = _health_surfaces(root)
        except Exception:
            pass
    return summary
