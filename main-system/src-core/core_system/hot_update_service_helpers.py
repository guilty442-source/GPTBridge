"""Shared helpers for HotUpdateService and its mixins (A185 split).

Module-level functions and constants used by the hot-update service
and its mixin modules, kept here to avoid circular imports.
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
    # Extension-hosting package: re-running its __init__ re-executes the
    # native loader and can map a second _sovereign_native copy into the
    # process (WER 2026-09-24: 0xc0000374 heap corruption after both the
    # dist-native and package-dir artifacts were loaded together).
    "core_system.native",
    "governance.sovereigns.",
)

RESOURCE_INDICATORS: Final[tuple[str, ...]] = (
    "close", "cleanup", "shutdown", "stop", "dispose",
    "teardown", "__del__", "_close", "_cleanup", "_shutdown",
)
_RESOURCE_INDICATORS = RESOURCE_INDICATORS

RELOAD_BATCH_SIZE: Final[int] = 16
RELOAD_BATCH_DELAY: Final[float] = 0.1
IDLE_WAIT_TIMEOUT: Final[float] = 10.0
POST_RELOAD_HEALTH_TIMEOUT: Final[float] = 5.0
_RELOAD_BATCH_SIZE = RELOAD_BATCH_SIZE
_RELOAD_BATCH_DELAY = RELOAD_BATCH_DELAY
_IDLE_WAIT_TIMEOUT = IDLE_WAIT_TIMEOUT
_POST_RELOAD_HEALTH_TIMEOUT = POST_RELOAD_HEALTH_TIMEOUT

_file_hash_cache: dict[str, tuple[float, str]] = {}


def is_protected(module_name: str) -> bool:
    for prefix in PROTECTED_MODULE_PREFIXES:
        prefix = prefix.rstrip(".")
        if module_name == prefix or module_name.startswith(prefix + "."):
            return True
    return False


def resource_cleanup_methods(module: types.ModuleType) -> list[tuple[Any, str]]:
    """Return resource-holding objects and their cleanup methods."""
    found: list[tuple[Any, str]] = []
    for value in vars(module).values():
        if value is None:
            continue
        if isinstance(
            value,
            (
                types.ModuleType, type, types.FunctionType,
                types.MethodType, types.BuiltinFunctionType,
                types.BuiltinMethodType,
            ),
        ):
            continue
        for method_name in _RESOURCE_INDICATORS:
            method = getattr(value, method_name, None)
            if callable(method):
                found.append((value, method_name))
    return found


def has_resources(module: types.ModuleType) -> bool:
    return bool(resource_cleanup_methods(module))


def cleanup_module(module: types.ModuleType) -> None:
    for value, method_name in resource_cleanup_methods(module):
        try:
            getattr(value, method_name)()
        except Exception:
            pass


def module_source_hash(file_path: str) -> str | None:
    """Return SHA-256 of a module's source file, or None if unreadable."""
    try:
        path = Path(file_path)
        mtime = path.stat().st_mtime
        if file_path in _file_hash_cache:
            cached_mtime, cached_hash = _file_hash_cache[file_path]
            if cached_mtime == mtime:
                return cached_hash
        hash_val = hashlib.sha256(path.read_bytes()).hexdigest()
        _file_hash_cache[file_path] = (mtime, hash_val)
        return hash_val
    except (OSError, ValueError):
        return None


def topological_sort(
    candidates: list[tuple[str, types.ModuleType]],
) -> list[tuple[str, types.ModuleType]]:
    """Sort candidates so dependencies reload first (DFS-based)."""
    candidate_names = {name for name, _ in candidates}
    name_to_module = {name: mod for name, mod in candidates}
    visited: set[str] = set()
    in_progress: set[str] = set()
    result: list[tuple[str, types.ModuleType]] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in in_progress:
            _logger.debug("hot_reload_cycle_detected module=%s", name)
            return
        in_progress.add(name)
        module = name_to_module.get(name)
        if module is not None:
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


def is_inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


__all__ = [
    "RELOADABLE_SRC_ROOTS",
    "PROTECTED_MODULE_PREFIXES",
    "RESOURCE_INDICATORS",
    "RELOAD_BATCH_SIZE",
    "RELOAD_BATCH_DELAY",
    "IDLE_WAIT_TIMEOUT",
    "POST_RELOAD_HEALTH_TIMEOUT",
    "is_protected",
    "resource_cleanup_methods",
    "has_resources",
    "cleanup_module",
    "module_source_hash",
    "topological_sort",
    "is_inside",
]
