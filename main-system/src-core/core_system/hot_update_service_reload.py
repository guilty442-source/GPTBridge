"""Hot-update reload mixin — A181/A182/A183 reload and prepare_generation.

Provides the reload_modules, _do_reload, _do_reload_locked, and
prepare_generation methods for the HotUpdateService class.
"""

from __future__ import annotations

import importlib
import types
from pathlib import Path
from typing import Any

from startup_core.feature_flags import get_flags

from core_system.hot_update_service_helpers import (
    _RELOAD_BATCH_SIZE,
    _RELOAD_BATCH_DELAY,
    _has_resources,
    _is_protected,
    _module_source_hash,
    _topological_sort,
)


class HotUpdateReloadMixin:
    """Reload and generation-preparation methods for HotUpdateService."""

    def reload_modules(
        self,
        *,
        governance: Any = None,
        approval_token: str | None = None,
        modules: Any = None,
    ) -> types.SimpleNamespace:
        """Reload governed backend modules after governance authorization.

        ``modules`` may be a list of dotted module names, or ``None`` to reload
        every loaded module that lives in the configured backend src roots.
        Returns a SimpleNamespace with ``ok``, ``reloaded``, ``skipped``,
        ``errors`` and ``error`` attributes.

        Per A181: ``HOT-RELOAD:reexecute-or-refresh-active-release-artifacts-only+
        verify-active-release-digests-before-and-after+release-identity-unchanged``.
        """
        authorized, auth_message = self._authenticate(governance, approval_token)
        if not authorized:
            return types.SimpleNamespace(
                ok=False,
                reloaded=[],
                skipped=[],
                errors=[auth_message],
                error=auth_message,
            )

        # A181: resolve active release pointer before reload.
        from .active_release import resolve_active_pointer
        pre_reload_pointer = resolve_active_pointer()

        roots = self._resolve_src_roots()

        requested: set[str] | None = None
        if modules is not None:
            try:
                requested = set(str(item).strip() for item in modules if item)
            except Exception:
                return types.SimpleNamespace(
                    ok=False,
                    reloaded=[],
                    skipped=[],
                    errors=["invalid-modules-argument"],
                    error="invalid-modules-argument",
                )

        # Run the reload through the circuit breaker for automatic
        # fallback on consecutive failures (A191/A192 parallel-update safety).
        result = self._circuit_breaker.call(
            self._do_reload,
            roots=roots,
            requested=requested,
        )

        # A181: verify active release pointer after reload.
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
            errors=result.errors if release_preserved else result.errors + ["active-release-identity-changed"],
            error=result.error if result.errors else ("" if release_preserved else "active-release-identity-changed"),
            pending_replacements=self.pending_replacement_count(),
        )

    def prepare_generation(
        self,
        *,
        governance: Any = None,
        approval_token: str | None = None,
        modules: Any = None,
        standby_validation: bool = False,
    ) -> types.SimpleNamespace:
        """Authenticate and preflight an immutable backend generation set.

        This performs no live-module mutation. The serving generation remains
        untouched while boot_core starts the candidate on the standby port.
        """
        if not standby_validation:
            authorized, auth_message = self._authenticate(
                governance, approval_token
            )
            if not authorized:
                return types.SimpleNamespace(
                    ok=False, hashes={}, error=auth_message
                )
        elif governance is None:
            return types.SimpleNamespace(
                ok=False, hashes={}, error="governance-unavailable"
            )
        requested = {str(item).strip() for item in (modules or ()) if item}
        if not requested:
            return types.SimpleNamespace(ok=False, hashes={}, error="empty-update-set")
        roots = self._resolve_src_roots()
        hashes: dict[str, str] = {}
        for module_name in sorted(requested):
            module = sys.modules.get(module_name)
            if not isinstance(module, types.ModuleType) or _is_protected(module_name):
                return types.SimpleNamespace(
                    ok=False, hashes={}, error=f"module-not-reloadable:{module_name}"
                )
            if not self._is_in_src_root(module, roots):
                return types.SimpleNamespace(
                    ok=False, hashes={}, error=f"module-outside-governed-root:{module_name}"
                )
            file_path = str(getattr(module, "__file__", ""))
            try:
                source = Path(file_path).read_text(encoding="utf-8")
                compile(source, file_path, "exec")
            except Exception as error:
                return types.SimpleNamespace(
                    ok=False,
                    hashes={},
                    error=f"{module_name}: preflight: {error}",
                )
            digest = _module_source_hash(file_path)
            if digest is None:
                return types.SimpleNamespace(
                    ok=False, hashes={}, error=f"hash-failed:{module_name}"
                )
            hashes[module_name] = digest
        return types.SimpleNamespace(ok=True, hashes=hashes, error="")

    def _do_reload(
        self,
        *,
        roots: list[Path],
        requested: set[str] | None,
    ) -> types.SimpleNamespace:
        if not roots:
            return types.SimpleNamespace(
                ok=False,
                reloaded=[],
                skipped=[],
                errors=["no-reloadable-src-roots"],
                error="no-reloadable-src-roots",
            )

        # Concurrent reload protection — only one reload at a time.
        if not self._reload_lock.acquire(blocking=False):
            return types.SimpleNamespace(
                ok=False,
                reloaded=[],
                skipped=[],
                errors=["reload-in-progress"],
                error="reload-in-progress",
            )

        try:
            return self._do_reload_locked(roots=roots, requested=requested)
        finally:
            self._reload_lock.release()

    def _do_reload_locked(
        self,
        *,
        roots: list[Path],
        requested: set[str] | None,
    ) -> types.SimpleNamespace:
        import sys
        import time
        import logging

        _logger = logging.getLogger("gptbridge.hot_update")

        # Wait for idle before mutating any module.
        if not self._wait_for_idle():
            _logger.warning(
                "hot_reload_idle_wait_timeout — proceeding with %d in-flight tasks",
                len(getattr(self.app, "_command_tasks", set())),
            )

        reloaded: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        snapshots: dict[str, dict[str, Any]] = {}

        candidates: list[tuple[str, types.ModuleType]] = []

        for module_name in list(sys.modules.keys()):
            if requested is not None and module_name not in requested:
                continue
            module = sys.modules[module_name]
            if not isinstance(module, types.ModuleType):
                continue
            if _is_protected(module_name):
                skipped.append(module_name)
                continue
            if not self._is_in_src_root(module, roots):
                if requested is not None and module_name in requested:
                    skipped.append(module_name)
                continue
            candidates.append((module_name, module))

        # Source hash filter — only reload modules whose source actually changed.
        changed = self._filter_changed(candidates)
        changed_set = {(name, mod) for name, mod in changed}
        skipped.extend(name for name, mod in candidates if (name, mod) not in changed_set)

        if not changed:
            return types.SimpleNamespace(
                ok=True,
                reloaded=[],
                skipped=skipped,
                errors=[],
                error="",
            )

        # Topological sort — dependencies reload before dependents.
        ordered = _topological_sort(changed)

        # Fail before mutating any live module when one changed source cannot compile.
        for module_name, module in ordered:
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                source = Path(file_path).read_text(encoding="utf-8")
                compile(source, str(file_path), "exec")
            except Exception as error:
                return types.SimpleNamespace(
                    ok=False,
                    reloaded=[],
                    skipped=[name for name, _module in ordered],
                    errors=[f"{module_name}: preflight: {error}"],
                    error=f"{module_name}: preflight: {error}",
                )

        # A191/A192: classify candidates into safe and resource-holding.
        resource_aware = get_flags().is_enabled("resource_aware_hot_update")
        safe_candidates: list[tuple[str, types.ModuleType]] = []
        resource_candidates: list[tuple[str, types.ModuleType]] = []
        for module_name, module in ordered:
            if resource_aware and _has_resources(module):
                resource_candidates.append((module_name, module))
            else:
                safe_candidates.append((module_name, module))

        # Phase 1: progressive batch reload of safe (stateless) modules.
        batch_errors = False
        for i in range(0, len(safe_candidates), _RELOAD_BATCH_SIZE):
            batch = safe_candidates[i:i + _RELOAD_BATCH_SIZE]
            for module_name, module in batch:
                snapshots[module_name] = dict(module.__dict__)
                try:
                    importlib.reload(module)
                    reloaded.append(module_name)
                except Exception as error:
                    errors.append(f"{module_name}: {error}")
                    skipped.append(module_name)
                    batch_errors = True
                    break
            if batch_errors:
                break
            # Inter-batch settle delay.
            if i + _RELOAD_BATCH_SIZE < len(safe_candidates):
                time.sleep(_RELOAD_BATCH_DELAY)

        if errors:
            # Roll back the whole attempted generation.
            for module_name, state in snapshots.items():
                module = sys.modules.get(module_name)
                if not isinstance(module, types.ModuleType):
                    continue
                module.__dict__.clear()
                module.__dict__.update(state)
            reloaded = []

        # Post-reload health verification.
        if reloaded and not errors:
            if not self._post_reload_health_check():
                _logger.warning(
                    "hot_reload_health_check_failed_after_reload — rolling back %d modules",
                    len(reloaded),
                )
                for module_name, state in snapshots.items():
                    module = sys.modules.get(module_name)
                    if not isinstance(module, types.ModuleType):
                        continue
                    module.__dict__.clear()
                    module.__dict__.update(state)
                reloaded = []
                errors.append("post-reload-health-check-failed")

        # Phase 2: queue resource-holding modules for idle-period replacement.
        if resource_aware and not errors:
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
            skipped.extend(name for name, _ in resource_candidates)

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
