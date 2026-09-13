"""Helper functions and constants for hot_update_service — A181/A182/A183.

Provides protected-module checks, resource detection, source hashing,
and topological sorting for the hot-reload subsystem.
"""

from __future__ import annotations

import hashlib
import logging
import types
from pathlib import Path
from typing import Any, Final

_logger = logging.getLogger("gptbridge.hot_update")

RELOADABLE_SRC_ROOTS: Final[tuple[str, ...]] = (
    "main-system/src-core",
    "shared-layer/src",
)

PROTECTED_MODULE_PREFIXES: Final[tuple[str, ...]] = (
    "governance_rule.codex.",
    "governance_rule.permission_directory.",
    "core_system.hot_update_service",
    "governance.sovereigns.",
    "governance.sub_sovereigns.",
)

# Methods/attributes that indicate a module holds live resources.
_RESOURCE_INDICATORS: Final[tuple[str, ...]] = (
    "close", "cleanup", "shutdown", "stop", "dispose",
    "teardown", "__del__", "_close", "_cleanup", "_shutdown",
)

# Batch size for progressive reload.
_RELOAD_BATCH_SIZE: Final[int] = 16
# Warm-up delay after each batch (seconds).
_RELOAD_BATCH_DELAY: Final[float] = 0.1
# Maximum wait for idle before timing out a reload request (seconds).
_IDLE_WAIT_TIMEOUT: Final[float] = 10.0
# Health check timeout after reload (seconds).
_POST_RELOAD_HEALTH_TIMEOUT: Final[float] = 5.0


def _is_protected(module_name: str) -> bool:
    return any(module_name == prefix or module_name.startswith(prefix + ".") for prefix in PROTECTED_MODULE_PREFIXES)


def _resource_cleanup_methods(module: types.ModuleType) -> list[tuple[Any, str]]:
    """Return resource-holding objects and their cleanup methods.

    Only bound methods on non-callable, non-module, non-class objects are
    considered, so module-level functions named ``close`` or ``shutdown``
    do not classify a module as resource-holding.
    """
    found: list[tuple[Any, str]] = []
    for value in vars(module).values():
        if value is None:
            continue
        if isinstance(
            value,
            (
                types.ModuleType,
                type,
                types.FunctionType,
                types.MethodType,
                types.BuiltinFunctionType,
                types.BuiltinMethodType,
            ),
        ):
            continue
        for method_name in _RESOURCE_INDICATORS:
            method = getattr(value, method_name, None)
            if callable(method):
                found.append((value, method_name))
    return found


def _has_resources(module: types.ModuleType) -> bool:
    """Return True if the module's namespace contains resource-holding objects."""
    return bool(_resource_cleanup_methods(module))


def _cleanup_module(module: types.ModuleType) -> None:
    """Best-effort cleanup of a module's resources before reload."""
    for value, method_name in _resource_cleanup_methods(module):
        try:
            getattr(value, method_name)()
        except Exception:
            pass


# Module-level cache for file hashes
_file_hash_cache: dict[str, tuple[float, str]] = {}  # path -> (mtime, hash)

def _module_source_hash(file_path: str) -> str | None:
    """Return SHA-256 of a module's source file, or None if unreadable (with caching)."""
    try:
        path = Path(file_path)
        mtime = path.stat().st_mtime
        # Check cache
        if file_path in _file_hash_cache:
            cached_mtime, cached_hash = _file_hash_cache[file_path]
            if cached_mtime == mtime:
                return cached_hash
        # Compute new hash
        hash_val = hashlib.sha256(path.read_bytes()).hexdigest()
        _file_hash_cache[file_path] = (mtime, hash_val)
        return hash_val
    except (OSError, ValueError):
        return None


def _topological_sort(
    candidates: list[tuple[str, types.ModuleType]],
) -> list[tuple[str, types.ModuleType]]:
    """Sort candidates so that dependencies (imported modules) reload first.

    Uses a simple DFS-based topological sort on the import graph.  If a
    cycle is detected, the cycle members are kept in their original order
    (cycle-safe: Python's importlib.reload handles re-entrant imports).
    """
    candidate_names = {name for name, _ in candidates}
    name_to_module = {name: mod for name, mod in candidates}
    visited: set[str] = set()
    in_progress: set[str] = set()
    result: list[tuple[str, types.ModuleType]] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in in_progress:
            # Cycle detected — skip to avoid infinite recursion.
            _logger.debug("hot_reload_cycle_detected module=%s", name)
            return
        in_progress.add(name)
        module = name_to_module.get(name)
        if module is not None:
            # Visit imported modules first (dependencies before dependents).
            for attr_name in dir(module):
                attr = getattr(module, attr_name, None)
                if isinstance(attr, types.ModuleType):
                    dep_name = getattr(attr, "__name__", "")
                    if dep_name in candidate_names and dep_name != name:
                        visit(dep_name)
        in_progress.discard(name)
        visited.add(name)
        if module is not None:
            result.append((name, module))

    for name, _ in candidates:
        visit(name)

    return result


def _is_inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


__all__ = [
    "RELOADABLE_SRC_ROOTS",
    "PROTECTED_MODULE_PREFIXES",
    "_RESOURCE_INDICATORS",
    "_RELOAD_BATCH_SIZE",
    "_RELOAD_BATCH_DELAY",
    "_IDLE_WAIT_TIMEOUT",
    "_POST_RELOAD_HEALTH_TIMEOUT",
    "_is_protected",
    "_resource_cleanup_methods",
    "_has_resources",
    "_cleanup_module",
    "_module_source_hash",
    "_topological_sort",
    "_is_inside",
]
