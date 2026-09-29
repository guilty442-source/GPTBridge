"""Worker-pool module-level support (A185 split).

Extracted from ``worker_pool.py`` (source-size contract): pool identity
constants, the governed capability-gate command adapter, pool directory
resolution, governed config loading and the shared clock.  ``worker_pool``
and ``worker_pool_alloc`` both consume this module; ``worker_pool``
re-exports every name for compatibility.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .git_repository import GitRepository
from .paths import EPHEMERAL_WORKTREES_RELATIVE, contained
from .worker_pool_types import PoolConfig

CONFIG_FILE = "config.json"
CONFLICT_STATS_FILE = "conflict_stats.json"
WORKER_POOL_ACTOR = "governance/worker-pool"

#: Retirement keeps the documented audited legacy adapter (see
#: ``_remove_worktree``); the boolean is a policy statement, not a literal
#: approval at the gateway.
_RETIREMENT_LEGACY_APPROVAL: bool = True


def _governed_worker_command(repo: GitRepository, args: list[str]):
    """Run one worker-allocation write through the capability gate."""
    from .capability_gate import execute_system_safe

    return execute_system_safe(
        list(args), actor=WORKER_POOL_ACTOR, repo_path=repo.path,
    )


def _pool_dir(root: str | Path) -> Path:
    repo = GitRepository(root)
    result = repo.run(["rev-parse", "--git-common-dir"])
    common = Path((result.stdout or "").strip())
    if not common.is_absolute():
        common = repo.path / common
    directory = common.resolve() / "gptbridge-automation" / "worker-pool"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def load_pool_config(root: str | Path) -> PoolConfig:
    """Governed pool config: deployment JSON overrides dataclass
    defaults (A386 — values resolved, not scattered)."""
    path = _pool_dir(root) / CONFIG_FILE
    data: dict[str, Any] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    config = PoolConfig()
    for key in PoolConfig.__dataclass_fields__:
        if key in data:
            setattr(config, key, data[key])
    if config.workers_root:
        config.workers_root = str(
            contained(root, config.workers_root, purpose="workers-root")
        )
    else:
        config.workers_root = str(
            contained(root, EPHEMERAL_WORKTREES_RELATIVE, purpose="workers-root")
        )
    return config


def _now() -> float:
    return time.time()


__all__ = [
    "CONFIG_FILE", "CONFLICT_STATS_FILE", "WORKER_POOL_ACTOR",
    "load_pool_config",
    "_RETIREMENT_LEGACY_APPROVAL", "_governed_worker_command", "_now",
    "_pool_dir",
]
