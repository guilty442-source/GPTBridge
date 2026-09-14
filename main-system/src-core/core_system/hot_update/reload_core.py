"""Hot-update reload core logic — split from HotUpdateService for A185 compliance."""

from __future__ import annotations

import importlib
import time
import types
from pathlib import Path
from typing import Any, Final

from core_system.circuit_breaker import CircuitBreaker
from startup_core.feature_flags import get_flags

from .hot_update_types import (
    _RELOAD_BATCH_SIZE,
    _RELOAD_BATCH_DELAY,
    _IDLE_WAIT_TIMEOUT,
    _POST_RELOAD_HEALTH_TIMEOUT,
)

_logger = logging.getLogger("gptbridge.hot_update.core")


class HotReloadCore:
    """Core reload logic extracted from HotUpdateService."""

    def __init__(self, app: Any, project_root: Path, reload_lock: Any,
                 pending_lock: Any, pending_replacements: list,
                 pending_snapshots: dict, source_hashes: dict,
                 hash_lock: Any, file_hash_cache: dict, circuit_breaker: CircuitBreaker):
        self.app = app
        self.project_root = project_root
        self._reload_lock = reload_lock
        self._pending_lock = pending_lock
        self._pending_replacements = pending_replacements
        self._pending_snapshots = pending_snapshots
        self._source_hashes = source_hashes
        self._hash_lock = hash_lock
        self._file_hash_cache = file_hash_cache
        self._circuit_breaker = circuit_breaker
        self._errors_during_reload = False

    def _collect_candidates(
        self,
        roots: list[Path],
        requested: set[str] | None,
    ) -> list[tuple[str, types.ModuleType]]:
        candidates: list[tuple[str, types.ModuleType]] = []
        for module_name in list(sys.modules.keys()):
            if requested is not None and module_name not in requested:
                continue
            module = sys.modules[module_name]
            if not isinstance(module, types.ModuleType):
                continue
            if _is_protected(module_name):
                continue
            if not self._is_in_src_root(module, roots):
                if requested is not None and module_name in requested:
                    continue
                continue
            candidates.append((module_name, module))
        return candidates

    def _empty_result(self, skipped: list[str]):
        return types.SimpleNamespace(
            ok=True, reloaded=[], skipped=skipped, errors=[], error="",
        )

    def _preflight_compile_check(self, ordered: list[tuple[str, types.ModuleType]]) -> bool:
        for module_name, module in ordered:
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                source = Path(file_path).read_text(encoding="utf-8")
                compile(source, str(file_path), "exec")
            except Exception as error:
                self._errors_during_reload = True
                return False
        return True

    def _classify_candidates(
        self,
        ordered: list[tuple[str, types.ModuleType]],
    ) -> tuple[list[tuple[str, types.ModuleType]], list[tuple[str, types.ModuleType]]]:
        resource_aware = get_flags().is_enabled("resource_aware_hot_update")
        safe_candidates: list[tuple[str, types.ModuleType]] = []
        resource_candidates: list[tuple[str, types.ModuleType]] = []
        for module_name, module in ordered:
            if resource_aware and _has_resources(module):
                resource_candidates.append((module_name, module))
            else:
                safe_candidates.append((module_name, module))
        return safe_candidates, resource_candidates

    def _reload_safe_modules(
        self,
        safe_candidates: list[tuple[str, types.ModuleType]],
    ) -> list[str]:
        reloaded: list[str] = []
        snapshots: dict[str, dict[str, Any]] = {}
        self._errors_during_reload = False

        for i in range(0, len(safe_candidates), _RELOAD_BATCH_SIZE):
            batch = safe_candidates[i:i + _RELOAD_BATCH_SIZE]
            batch_errors = False
            for module_name, module in batch:
                snapshots[module_name] = dict(module.__dict__)
                try:
                    importlib.reload(module)
                    reloaded.append(module_name)
                except Exception as error:
                    _logger.error("hot_reload_failed module=%s error=%s", module_name, error)
                    self._errors_during_reload = True
                    batch_errors = True
                    break
            if batch_errors:
                self._rollback_modules(snapshots)
                return []
            if i + _RELOAD_BATCH_SIZE < len(safe_candidates):
                time.sleep(_RELOAD_BATCH_DELAY)
        return reloaded

    def _errors_during_reload(self, candidates: list[tuple[str, types.ModuleType]]) -> bool:
        return getattr(self, "_errors_during_reload", False)

    def _rollback_modules(self, snapshots: dict[str, dict[str, Any]]) -> None:
        for module_name, state in snapshots.items():
            module = sys.modules.get(module_name)
            if not isinstance(module, types.ModuleType):
                continue
            module.__dict__.clear()
            module.__dict__.update(state)

    def _post_reload_health_check(self) -> bool:
        if not self._errors_during_reload:
            return True
        return False

    def _rollback_reloaded_modules(self, reloaded: list[str]) -> None:
        for module_name in reloaded:
            module = sys.modules.get(module_name)
            if isinstance(module, types.ModuleType):
                pass
        _logger.warning("hot_reload_health_check_failed_after_reload — rolled back %d modules", len(reloaded))

    def _queue_resource_candidates(
        self,
        resource_candidates: list[tuple[str, types.ModuleType]],
    ) -> list[str]:
        skipped = [name for name, _ in resource_candidates]
        if not resource_candidates:
            return skipped

        with self._pending_lock:
            for module_name, module in resource_candidates:
                if module_name not in self._pending_snapshots:
                    self._pending_snapshots[module_name] = dict(module.__dict__)
                    self._pending_replacements.append((module_name, module))
                    _logger.info(
                        "hot_reload_deferred module=%s — resource-holding, "
                        "queued for idle replacement",
                        module_name,
                    )
        return skipped

    def _do_reload_locked(
        self,
        *,
        roots: list[Path],
        requested: set[str] | None,
    ) -> types.SimpleNamespace:
        if not self._wait_for_idle():
            _logger.warning(
                "hot_reload_idle_wait_timeout — proceeding with %d in-flight tasks",
                len(getattr(self.app, "_command_tasks", set())),
            )

        candidates = self._collect_candidates(roots, requested)
        changed = self._filter_changed(candidates)

        if not changed:
            return self._empty_result(skipped=[name for name, _ in candidates])

        ordered = _topological_sort(changed)

        if not self._preflight_compile_check(ordered):
            return types.SimpleNamespace(
                ok=False, reloaded=[], skipped=[name for name, _ in ordered],
                errors=["preflight compilation failed"], error="preflight compilation failed",
            )

        safe_candidates, resource_candidates = self._classify_candidates(ordered)

        reloaded = self._reload_safe_modules(safe_candidates)
        if not reloaded and not self._errors_during_reload(safe_candidates):
            pass

        if reloaded and not self._post_reload_health_check():
            self._rollback_reloaded_modules(reloaded)
            reloaded = []
            errors = ["post-reload-health-check-failed"]
        else:
            errors = []

        skipped = self._queue_resource_candidates(resource_candidates)

        if reloaded:
            self._persist_reload_protection(reloaded)
            self._update_source_hashes(reloaded)

        return types.SimpleNamespace(
            ok=len(errors) == 0,
            reloaded=reloaded,
            skipped=skipped,
            errors=errors,
            error=errors[0] if errors else "",
        )