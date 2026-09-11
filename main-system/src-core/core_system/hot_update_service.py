from __future__ import annotations

import importlib
import hashlib
import json
import logging
import os
import sys
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from startup_core.feature_flags import get_flags
from core_system.circuit_breaker import CircuitBreaker, CircuitOpenError

_logger = logging.getLogger("gptbridge.hot_update")

RELOADABLE_SRC_ROOTS: Final[tuple[str, ...]] = (
    "main-system/src-core",
    "shared-layer/src",
)

PROTECTED_MODULE_PREFIXES: Final[tuple[str, ...]] = (
    "governance_rule.codex.",
    "governance_rule.permission_directory.",
    "core_system.hot_update_service",
    "core_system.maintenance_sovereign",
)

# Methods/attributes that indicate a module holds live resources.
# If any of these are present in the module's namespace (as callables
# or objects with cleanup semantics), the module is classified as
# resource-holding and deferred to idle-period replacement.
_RESOURCE_INDICATORS: Final[tuple[str, ...]] = (
    "close", "cleanup", "shutdown", "stop", "dispose",
    "teardown", "__del__", "_close", "_cleanup", "_shutdown",
)


def _is_protected(module_name: str) -> bool:
    return any(module_name == prefix or module_name.startswith(prefix + ".") for prefix in PROTECTED_MODULE_PREFIXES)


def _has_resources(module: types.ModuleType) -> bool:
    """Return True if the module's namespace contains resource-holding objects."""
    for attr_name in _RESOURCE_INDICATORS:
        attr = getattr(module, attr_name, None)
        if callable(attr):
            return True
    # Also check for common resource-holding patterns: objects with close().
    for value in vars(module).values():
        if callable(value):
            continue
        close_method = getattr(value, "close", None)
        if callable(close_method):
            return True
        shutdown_method = getattr(value, "shutdown", None)
        if callable(shutdown_method):
            return True
    return False


def _cleanup_module(module: types.ModuleType) -> None:
    """Best-effort cleanup of a module's resources before reload."""
    for method_name in ("close", "cleanup", "shutdown", "stop", "dispose", "teardown"):
        method = getattr(module, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
            break


class HotUpdateService:
    """Version-gated main-system hot-update boundary and system-wide hot-reload.

    Hot-update remains a frozen, version-gated boundary.  Hot-reload is a
    separate maintenance operation: it re-executes already-loaded governed
    backend modules in place so source edits take effect without a full process
    restart, after governance authorization.
    """

    def __init__(self, app: Any, interval_seconds: float = 1.0) -> None:
        self.app = app
        self.interval_seconds = max(0.5, interval_seconds)
        fallback_root = Path(__file__).resolve().parents[3]
        self.project_root = Path(
            getattr(app, "project_root", str(fallback_root))
        ).resolve()
        # Circuit breaker for hot-reload operations (A191/A192 safety).
        self._circuit_breaker = CircuitBreaker(
            name="hot-reload",
            failure_threshold=get_flags().get("circuit_breaker_hot_reload", "failure_threshold", 3),
            recovery_timeout_seconds=get_flags().get("circuit_breaker_hot_reload", "recovery_timeout_seconds", 30),
            fallback_fn=lambda **kw: types.SimpleNamespace(
                ok=False, reloaded=[], skipped=[], errors=["circuit-open"], error="circuit-open",
            ),
            flag_name="circuit_breaker_hot_reload",
        )
        # Pending resource-holding module replacements, deferred to idle.
        self._pending_replacements: list[tuple[str, types.ModuleType]] = []
        self._pending_snapshots: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

    def _resolve_src_roots(self) -> list[Path]:
        roots: list[Path] = []
        for relative in RELOADABLE_SRC_ROOTS:
            root = (self.project_root / relative).resolve()
            if root.is_dir():
                roots.append(root)
        return roots

    def _is_in_src_root(self, module: types.ModuleType, roots: list[Path]) -> bool:
        file_path = getattr(module, "__file__", None)
        if not file_path:
            return False
        try:
            resolved = Path(file_path).resolve()
        except (OSError, ValueError):
            return False
        return any(_is_inside(resolved, root) for root in roots)

    def _authenticate(self, governance: Any, approval_token: str | None) -> tuple[bool, str]:
        if governance is None:
            return False, "governance-unavailable"
        if not approval_token:
            return False, "missing-approval-token"
        auth = getattr(governance, "_authentication", None)
        if auth is None or not hasattr(auth, "authenticate_token"):
            return False, "governance-authentication-unavailable"
        try:
            claims = auth.authenticate_token(approval_token)
        except Exception as error:
            return False, f"permission-denied: {error}"
        if getattr(claims, "capability", "") != "hot-update" and getattr(claims, "capability", "") != "hot-reload":
            return False, "capability-mismatch"
        return True, ""

    def _persist_reload_protection(self, module_names: list[str]) -> None:
        """Pin successfully reloaded source revisions against auto-repair."""
        protected: dict[str, str] = {}
        for module_name in module_names:
            module = sys.modules.get(module_name)
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                path = Path(file_path).resolve()
                relative = path.relative_to(self.project_root).as_posix()
                protected[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, ValueError):
                continue
        if not protected:
            return
        target = (
            self.project_root / "main-system" / "runtime" / "state"
            / "hot-reload-protection.json"
        )
        payload = {
            "version": 1,
            "protected_sources": protected,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError:
            pass

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
        The active release pointer is resolved before reload to establish the
        baseline, and verified after reload to confirm the release identity is
        preserved.
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

        # A181: verify active release pointer after reload — release identity
        # must be unchanged.  If the pointer was present before reload, it
        # must still be present and identical after reload.
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

        # Fail before mutating any live module when one changed source cannot
        # compile. This keeps the currently serving generation intact.
        for module_name, module in candidates:
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
                    skipped=[name for name, _module in candidates],
                    errors=[f"{module_name}: preflight: {error}"],
                    error=f"{module_name}: preflight: {error}",
                )

        # A191/A192: classify candidates into safe (stateless) and
        # resource-holding.  Safe modules reload immediately.  Resource-
        # holding modules are deferred to idle-period replacement so
        # in-flight requests are never left with a half-cleaned module.
        resource_aware = get_flags().is_enabled("resource_aware_hot_update")
        safe_candidates: list[tuple[str, types.ModuleType]] = []
        resource_candidates: list[tuple[str, types.ModuleType]] = []
        for module_name, module in candidates:
            if resource_aware and _has_resources(module):
                resource_candidates.append((module_name, module))
            else:
                safe_candidates.append((module_name, module))

        # Phase 1: immediately reload safe (stateless) modules.
        for module_name, module in safe_candidates:
            snapshots[module_name] = dict(module.__dict__)
            try:
                importlib.reload(module)
                reloaded.append(module_name)
            except Exception as error:
                errors.append(f"{module_name}: {error}")
                skipped.append(module_name)
                break

        if errors:
            # Roll back the whole attempted generation, including modules that
            # reloaded before the failing dependency.
            for module_name, state in snapshots.items():
                module = sys.modules.get(module_name)
                if not isinstance(module, types.ModuleType):
                    continue
                module.__dict__.clear()
                module.__dict__.update(state)
            reloaded = []

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

        return types.SimpleNamespace(
            ok=len(errors) == 0,
            reloaded=reloaded,
            skipped=skipped,
            errors=errors,
            error=errors[0] if errors else "",
        )

    def is_idle(self) -> bool:
        """Return True when the system has no in-flight command tasks.

        This is the gate for applying deferred resource-holding module
        replacements — they must only run when no request is being served.
        """
        command_tasks = getattr(self.app, "_command_tasks", None)
        if command_tasks is None:
            return True
        return len(command_tasks) == 0

    def pending_replacement_count(self) -> int:
        """Return the number of resource-holding modules awaiting replacement."""
        with self._pending_lock:
            return len(self._pending_replacements)

    def apply_pending_replacements(self) -> types.SimpleNamespace:
        """Apply deferred resource-holding module replacements during idle.

        Should be called from a periodic idle-check loop.  Only runs when
        ``is_idle()`` is True.  Each module is cleaned up before reload so
        resources (sockets, DB connections) are released gracefully.

        Returns a SimpleNamespace with ``applied``, ``errors``, and
        ``skipped`` attributes.
        """
        applied: list[str] = []
        errors: list[str] = []
        skipped: list[str] = []

        if not self.is_idle():
            return types.SimpleNamespace(
                applied=[], errors=[], skipped=[], idle=False,
            )

        with self._pending_lock:
            pending = list(self._pending_replacements)
            self._pending_replacements.clear()

        for module_name, old_module in pending:
            # Re-check: the module may have been reloaded by another path.
            current = sys.modules.get(module_name)
            if current is None or current is not old_module:
                skipped.append(module_name)
                continue
            try:
                # Graceful cleanup before reload.
                _cleanup_module(current)
                importlib.reload(current)
                applied.append(module_name)
                _logger.info(
                    "hot_reload_applied_deferred module=%s", module_name,
                )
            except Exception as error:
                errors.append(f"{module_name}: {error}")
                # Roll back to the snapshot.
                snapshot = self._pending_snapshots.pop(module_name, None)
                if snapshot and isinstance(current, types.ModuleType):
                    current.__dict__.clear()
                    current.__dict__.update(snapshot)

        # Clean up snapshots for successfully applied modules.
        for name in applied:
            self._pending_snapshots.pop(name, None)

        if applied:
            self._persist_reload_protection(applied)

        return types.SimpleNamespace(
            applied=applied,
            errors=errors,
            skipped=skipped,
            idle=True,
        )

    def start(self) -> None:
        """No in-process update loop; hot-reload is triggered explicitly."""
        return None

    async def stop(self) -> None:
        return None

    async def _apply(
        self,
        _plan: dict[str, Any],
        *,
        repairs_completed: bool = False,
    ) -> None:
        del repairs_completed
        raise PermissionError("PERMISSION_DENIED")


def _is_inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


__all__ = ["HotUpdateService"]
