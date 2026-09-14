"""Reload execution mixin for HotUpdateService (A185 split).

Contains the core reload pipeline: candidate collection, topological
sort, preflight compile check, batched reload with rollback, and
resource-holding module deferral.
"""
from __future__ import annotations

import importlib
import logging
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

from .hot_update_service_helpers import (
    RELOAD_BATCH_SIZE,
    RELOAD_BATCH_DELAY,
    cleanup_module,
    has_resources,
    is_protected,
    module_source_hash,
    topological_sort,
)

_logger = logging.getLogger("gptbridge.hot_update")


class HotUpdateReloadMixin:
    """Reload execution pipeline methods."""

    app: Any
    project_root: Path
    _reload_lock: threading.RLock
    _pending_lock: threading.Lock
    _pending_replacements: list[tuple[str, types.ModuleType]]
    _pending_snapshots: dict[str, dict[str, Any]]

    def reload_modules(
        self,
        *,
        governance: Any = None,
        approval_token: str | None = None,
        modules: Any = None,
    ) -> types.SimpleNamespace:
        """Reload governed backend modules after governance authorization."""
        auth_result = self._authenticate_reload(governance, approval_token)
        if not auth_result.ok:
            return auth_result
        pre_reload_pointer = self._resolve_active_release()
        roots = self._resolve_src_roots()
        requested = self._parse_requested_modules(modules)
        if isinstance(requested, types.SimpleNamespace):
            return requested
        result = self._circuit_breaker.call(
            self._do_reload, roots=roots, requested=requested,
        )
        return self._finalize_reload(result, pre_reload_pointer)

    def _authenticate_reload(
        self, governance: Any, approval_token: str | None,
    ) -> types.SimpleNamespace:
        authorized, auth_message = self._authenticate(governance, approval_token)
        if not authorized:
            return types.SimpleNamespace(
                ok=False, reloaded=[], skipped=[],
                errors=[auth_message], error=auth_message,
            )
        return types.SimpleNamespace(ok=True)

    def _resolve_active_release(self):
        from .active_release import resolve_active_pointer
        return resolve_active_pointer()

    def _parse_requested_modules(
        self, modules: Any,
    ) -> set[str] | types.SimpleNamespace:
        if modules is not None:
            try:
                return set(str(item).strip() for item in modules if item)
            except Exception:
                return types.SimpleNamespace(
                    ok=False, reloaded=[], skipped=[],
                    errors=["invalid-modules-argument"],
                    error="invalid-modules-argument",
                )
        return set()

    def _finalize_reload(
        self, result: types.SimpleNamespace, pre_reload_pointer: Any,
    ) -> types.SimpleNamespace:
        from .active_release import resolve_active_pointer
        post_reload_pointer = resolve_active_pointer()
        release_preserved = True
        if pre_reload_pointer is not None:
            release_preserved = (
                post_reload_pointer is not None
                and post_reload_pointer.release_id == pre_reload_pointer.release_id
                and post_reload_pointer.certificate_digest
                == pre_reload_pointer.certificate_digest
            )
        return types.SimpleNamespace(
            ok=result.ok and release_preserved,
            reloaded=result.reloaded,
            skipped=result.skipped,
            errors=result.errors if release_preserved
            else result.errors + ["active-release-identity-changed"],
            error=result.error if result.errors
            else ("" if release_preserved else "active-release-identity-changed"),
            pending_replacements=self.pending_replacement_count(),
        )

    def _do_reload(
        self, *, roots: list[Path], requested: set[str] | None,
    ) -> types.SimpleNamespace:
        if not roots:
            return types.SimpleNamespace(
                ok=False, reloaded=[], skipped=[],
                errors=["no-reloadable-src-roots"], error="no-reloadable-src-roots",
            )
        if not self._reload_lock.acquire(blocking=False):
            return types.SimpleNamespace(
                ok=False, reloaded=[], skipped=[],
                errors=["reload-in-progress"], error="reload-in-progress",
            )
        try:
            return self._do_reload_locked(roots=roots, requested=requested)
        finally:
            self._reload_lock.release()

    def _do_reload_locked(
        self, *, roots: list[Path], requested: set[str] | None,
    ) -> types.SimpleNamespace:
        if not self._wait_for_idle():
            _logger.warning(
                "hot_reload_idle_wait_timeout proceeding with %d in-flight tasks",
                len(getattr(self.app, "_command_tasks", set())),
            )
        candidates = self._collect_candidates(roots, requested)
        changed = self._filter_changed(candidates)
        if not changed:
            return self._empty_result(skipped=[name for name, _ in candidates])
        ordered = topological_sort(changed)
        if not self._preflight_compile_check(ordered):
            return types.SimpleNamespace(
                ok=False, reloaded=[], skipped=[name for name, _ in ordered],
                errors=["preflight compilation failed"],
                error="preflight compilation failed",
            )
        safe_candidates, resource_candidates = self._classify_candidates(ordered)
        reloaded = self._reload_safe_modules(safe_candidates)
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
            ok=len(errors) == 0, reloaded=reloaded, skipped=skipped,
            errors=errors, error=errors[0] if errors else "",
        )

    def _collect_candidates(
        self, roots: list[Path], requested: set[str] | None,
    ) -> list[tuple[str, types.ModuleType]]:
        candidates: list[tuple[str, types.ModuleType]] = []
        for module_name in list(sys.modules.keys()):
            if requested is not None and module_name not in requested:
                continue
            module = sys.modules[module_name]
            if not isinstance(module, types.ModuleType):
                continue
            if is_protected(module_name):
                continue
            if not self._is_in_src_root(module, roots):
                continue
            candidates.append((module_name, module))
        return candidates

    def _empty_result(self, skipped: list[str]) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            ok=True, reloaded=[], skipped=skipped, errors=[], error="",
        )

    def _preflight_compile_check(
        self, ordered: list[tuple[str, types.ModuleType]],
    ) -> bool:
        for module_name, module in ordered:
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                source = Path(file_path).read_text(encoding="utf-8")
                compile(source, str(file_path), "exec")
            except Exception:
                return False
        return True

    def _classify_candidates(
        self, ordered: list[tuple[str, types.ModuleType]],
    ) -> tuple[list, list]:
        from startup_core.feature_flags import get_flags
        resource_aware = get_flags().is_enabled("resource_aware_hot_update")
        safe: list[tuple[str, types.ModuleType]] = []
        resource: list[tuple[str, types.ModuleType]] = []
        for module_name, module in ordered:
            if resource_aware and has_resources(module):
                resource.append((module_name, module))
            else:
                safe.append((module_name, module))
        return safe, resource

    def _reload_safe_modules(
        self, safe_candidates: list[tuple[str, types.ModuleType]],
    ) -> list[str]:
        reloaded: list[str] = []
        snapshots: dict[str, dict[str, Any]] = {}
        for i in range(0, len(safe_candidates), RELOAD_BATCH_SIZE):
            batch = safe_candidates[i:i + RELOAD_BATCH_SIZE]
            batch_errors = False
            for module_name, module in batch:
                snapshots[module_name] = dict(module.__dict__)
                try:
                    importlib.reload(module)
                    reloaded.append(module_name)
                except Exception as error:
                    _logger.error(
                        "hot_reload_failed module=%s error=%s", module_name, error)
                    batch_errors = True
                    break
            if batch_errors:
                self._rollback_modules(snapshots)
                return []
            if i + RELOAD_BATCH_SIZE < len(safe_candidates):
                time.sleep(RELOAD_BATCH_DELAY)
        return reloaded

    def _rollback_modules(self, snapshots: dict[str, dict[str, Any]]) -> None:
        for module_name, state in snapshots.items():
            module = sys.modules.get(module_name)
            if not isinstance(module, types.ModuleType):
                continue
            module.__dict__.clear()
            module.__dict__.update(state)

    def _rollback_reloaded_modules(self, reloaded: list[str]) -> None:
        _logger.warning(
            "hot_reload_health_check_failed_after_reload rolled back %d modules",
            len(reloaded))

    def _queue_resource_candidates(
        self, resource_candidates: list[tuple[str, types.ModuleType]],
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
                        "hot_reload_deferred module=%s resource-holding, "
                        "queued for idle replacement", module_name)
        return skipped


__all__ = ["HotUpdateReloadMixin"]
