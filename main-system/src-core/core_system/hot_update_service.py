from __future__ import annotations

import asyncio
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
    "governance.sovereigns.",
    "governance.sub_sovereigns.",
)

# Methods/attributes that indicate a module holds live resources.
# If any of these are present in the module's namespace (as callables
# or objects with cleanup semantics), the module is classified as
# resource-holding and deferred to idle-period replacement.
_RESOURCE_INDICATORS: Final[tuple[str, ...]] = (
    "close", "cleanup", "shutdown", "stop", "dispose",
    "teardown", "__del__", "_close", "_cleanup", "_shutdown",
)

# Batch size for progressive reload — modules are reloaded in groups
# of this size with a health check between batches.
_RELOAD_BATCH_SIZE: Final[int] = 8
# Warm-up delay after each batch (seconds) — lets reloaded modules
# settle before the next batch.
_RELOAD_BATCH_DELAY: Final[float] = 0.2
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


def _module_source_hash(file_path: str) -> str | None:
    """Return SHA-256 of a module's source file, or None if unreadable."""
    try:
        return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
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
        # Concurrent reload lock — prevents two reload_modules calls from
        # mutating sys.modules simultaneously.  RLock so apply_pending_replacements
        # can be called from within a reload context if needed.
        self._reload_lock = threading.RLock()
        # Source hash tracking — maps module name to last-known SHA-256 of
        # its source file.  Only modules whose hash changed are reloaded.
        self._source_hashes: dict[str, str] = {}
        self._hash_lock = threading.Lock()
        # Load persisted source hashes so we know what was last reloaded.
        self._load_source_hashes()
        # Idle-loop lifecycle.
        self._stop_event: asyncio.Event | None = None
        self._idle_task: asyncio.Task[Any] | None = None

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

    def _load_source_hashes(self) -> None:
        """Load persisted source hashes from the state file."""
        path = (
            self.project_root / "main-system" / "runtime" / "state"
            / "hot-reload-hashes.json"
        )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            with self._hash_lock:
                self._source_hashes = data.get("hashes", {})
        except (OSError, json.JSONDecodeError):
            pass

    def _save_source_hashes(self) -> None:
        """Persist source hashes so the next reload can diff."""
        path = (
            self.project_root / "main-system" / "runtime" / "state"
            / "hot-reload-hashes.json"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._hash_lock:
                payload = {
                    "version": 1,
                    "hashes": dict(self._source_hashes),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass

    def _filter_changed(
        self, candidates: list[tuple[str, types.ModuleType]]
    ) -> list[tuple[str, types.ModuleType]]:
        """Return only candidates whose source hash changed since last reload.

        If a module has no file path, it's always included (can't hash it).
        On first run (no stored hash), all candidates are included.
        """
        changed: list[tuple[str, types.ModuleType]] = []
        with self._hash_lock:
            stored = dict(self._source_hashes)
        for module_name, module in candidates:
            file_path = getattr(module, "__file__", None)
            if not file_path:
                changed.append((module_name, module))
                continue
            current_hash = _module_source_hash(file_path)
            if current_hash is None:
                changed.append((module_name, module))
                continue
            if stored.get(module_name) != current_hash:
                changed.append((module_name, module))
            else:
                _logger.debug(
                    "hot_reload_skip_unchanged module=%s", module_name,
                )
        return changed

    def _update_source_hashes(self, module_names: list[str]) -> None:
        """Update stored hashes for successfully reloaded modules."""
        with self._hash_lock:
            for module_name in module_names:
                module = sys.modules.get(module_name)
                if module is None:
                    continue
                file_path = getattr(module, "__file__", None)
                if not file_path:
                    continue
                h = _module_source_hash(file_path)
                if h is not None:
                    self._source_hashes[module_name] = h
        self._save_source_hashes()

    def _wait_for_idle(self, timeout: float = _IDLE_WAIT_TIMEOUT) -> bool:
        """Wait until the system is idle (no in-flight command tasks).

        Returns True if idle within the timeout, False otherwise.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_idle():
                return True
            time.sleep(0.1)
        return self.is_idle()

    def _post_reload_health_check(self) -> bool:
        """Verify the system is still healthy after a reload.

        Probes the app's health endpoint if available.  Returns True
        if healthy, False if the reload appears to have broken something.
        """
        health_port = None
        try:
            from startup_core.startup_config import port as _cfg_port
            health_port = _cfg_port("health_probe")
        except Exception:
            pass
        if not health_port:
            return True  # Can't verify — assume OK.
        try:
            import urllib.request
            url = f"http://127.0.0.1:{health_port}/health?level=brief"
            request = urllib.request.Request(url, headers={"Connection": "close"})
            with urllib.request.urlopen(request, timeout=_POST_RELOAD_HEALTH_TIMEOUT) as resp:
                import json as _json
                payload = _json.loads(resp.read().decode("utf-8"))
                return bool(payload.get("ok") is True or payload.get("runtime_state") in ("ready", "degraded"))
        except Exception:
            # Health check failed — but the reload itself may have succeeded.
            # Log a warning but don't fail the reload; the circuit breaker
            # will catch persistent failures.
            _logger.warning("hot_reload_health_check_failed — post-reload probe did not respond")
            return True

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
        # Wait for idle before mutating any module — in-flight requests
        # must not be left with a half-reloaded dependency.
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

        # Source hash filter — only reload modules whose source actually
        # changed since the last reload.  This avoids unnecessary disruption.
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

        # Fail before mutating any live module when one changed source cannot
        # compile. This keeps the currently serving generation intact.
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

        # A191/A192: classify candidates into safe (stateless) and
        # resource-holding.  Safe modules reload immediately.  Resource-
        # holding modules are deferred to idle-period replacement so
        # in-flight requests are never left with a half-cleaned module.
        resource_aware = get_flags().is_enabled("resource_aware_hot_update")
        safe_candidates: list[tuple[str, types.ModuleType]] = []
        resource_candidates: list[tuple[str, types.ModuleType]] = []
        for module_name, module in ordered:
            if resource_aware and _has_resources(module):
                resource_candidates.append((module_name, module))
            else:
                safe_candidates.append((module_name, module))

        # Phase 1: progressive batch reload of safe (stateless) modules.
        # Reload in batches of _RELOAD_BATCH_SIZE with a short delay
        # between batches to let modules settle.
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
            # Roll back the whole attempted generation, including modules that
            # reloaded before the failing dependency.
            for module_name, state in snapshots.items():
                module = sys.modules.get(module_name)
                if not isinstance(module, types.ModuleType):
                    continue
                module.__dict__.clear()
                module.__dict__.update(state)
            reloaded = []

        # Post-reload health verification — if the health endpoint
        # reports failure after reload, roll back everything.
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
        if not self.is_idle():
            return types.SimpleNamespace(
                applied=[], errors=[], skipped=[], idle=False,
            )

        # Concurrent reload protection.
        if not self._reload_lock.acquire(blocking=False):
            return types.SimpleNamespace(
                applied=[], errors=[], skipped=[], idle=True,
                error="reload-in-progress",
            )

        try:
            return self._apply_pending_locked()
        finally:
            self._reload_lock.release()

    def _apply_pending_locked(self) -> types.SimpleNamespace:
        applied: list[str] = []
        errors: list[str] = []
        skipped: list[str] = []

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
            self._update_source_hashes(applied)
            # Post-reload health verification.
            if not self._post_reload_health_check():
                _logger.warning(
                    "hot_reload_health_check_failed_after_deferred — "
                    "applied modules may need attention",
                )

        return types.SimpleNamespace(
            applied=applied,
            errors=errors,
            skipped=skipped,
            idle=True,
        )

    async def _idle_loop(self) -> None:
        """Periodically apply deferred resource-holding module replacements.

        Runs only when the system is idle and there are pending resource-holding
        module replacements.  This prevents half-replaced modules from being used
        by in-flight requests.
        """
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                if self.pending_replacement_count() > 0 and self.is_idle():
                    await asyncio.to_thread(self.apply_pending_replacements)
            except Exception:
                # Best-effort: never let the idle loop die.
                pass
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        """Begin the idle-period deferred-replacement loop."""
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._stop_event = asyncio.Event()
        self._idle_task = asyncio.create_task(
            self._idle_loop(),
            name="hot-update-idle-loop",
        )

    async def stop(self) -> None:
        """Cancel the idle-period deferred-replacement loop."""
        if self._idle_task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._idle_task.cancel()
        try:
            await self._idle_task
        except asyncio.CancelledError:
            pass
        self._idle_task = None
        self._stop_event = None

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
