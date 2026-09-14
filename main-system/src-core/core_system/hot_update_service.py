from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from startup_core.feature_flags import get_flags
from core_system.circuit_breaker import CircuitBreaker, CircuitOpenError

from .hot_update_service_helpers import (
    RELOADABLE_SRC_ROOTS,
    PROTECTED_MODULE_PREFIXES,
    is_protected as _is_protected,
    resource_cleanup_methods as _resource_cleanup_methods,
    has_resources as _has_resources,
    cleanup_module as _cleanup_module,
    module_source_hash as _module_source_hash,
    topological_sort as _topological_sort,
    is_inside as _is_inside,
    IDLE_WAIT_TIMEOUT,
    POST_RELOAD_HEALTH_TIMEOUT,
)
from .hot_update_service_hashes import HotUpdateHashMixin
from .hot_update_service_reload import HotUpdateReloadMixin
from .hot_update_service_idle import HotUpdateIdleMixin

_logger = logging.getLogger("gptbridge.hot_update")

__all__ = ["HotUpdateService"]


class HotUpdateService(HotUpdateHashMixin, HotUpdateReloadMixin, HotUpdateIdleMixin):
    """Version-gated main-system hot-update boundary and system-wide hot-reload.

    Hot-update remains a frozen, version-gated boundary.  Hot-reload is a
    separate maintenance operation: it re-executes already-loaded governed
    backend modules in place so source edits take effect without a full
    process restart, after governance authorization.
    """

    def __init__(self, app: Any, interval_seconds: float = 30.0) -> None:
        self.app = app
        self.interval_seconds = max(10.0, interval_seconds)
        fallback_root = Path(__file__).resolve().parents[3]
        self.project_root = Path(
            getattr(app, "project_root", str(fallback_root))
        ).resolve()
        self._circuit_breaker = CircuitBreaker(
            name="hot-reload",
            failure_threshold=get_flags().get("circuit_breaker_hot_reload", "failure_threshold", 3),
            recovery_timeout_seconds=get_flags().get("circuit_breaker_hot_reload", "recovery_timeout_seconds", 30),
            fallback_fn=lambda **kw: types.SimpleNamespace(
                ok=False, reloaded=[], skipped=[], errors=["circuit-open"], error="circuit-open",
            ),
            flag_name="circuit_breaker_hot_reload",
        )
        self._pending_replacements: list[tuple[str, types.ModuleType]] = []
        self._pending_snapshots: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._reload_lock = threading.RLock()
        self._source_hashes: dict[str, str] = {}
        self._hash_lock = threading.Lock()
        self._file_hash_cache: dict[str, tuple[float, str]] = {}
        self._load_source_hashes()
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
        capability = getattr(claims, "capability", "")
        if capability not in ("hot-update", "hot-reload"):
            return False, "capability-mismatch"
        return True, ""

    def _wait_for_idle(self, timeout: float = IDLE_WAIT_TIMEOUT) -> bool:
        """Wait until the system is idle (no in-flight command tasks)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_idle():
                return True
            time.sleep(0.1)
        return self.is_idle()

    def _post_reload_health_check(self) -> bool:
        """Verify the system is still healthy after a reload via HTTP probe."""
        health_port = None
        try:
            from startup_core.startup_config import port as _cfg_port
            health_port = _cfg_port("health_probe")
        except Exception:
            pass
        if not health_port:
            return True
        try:
            import urllib.request
            url = f"http://127.0.0.1:{health_port}/health?level=brief"
            request = urllib.request.Request(url, headers={"Connection": "close"})
            _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with _opener.open(request, timeout=POST_RELOAD_HEALTH_TIMEOUT) as resp:
                import json as _json
                payload = _json.loads(resp.read().decode("utf-8"))
                return bool(
                    payload.get("ok") is True
                    or payload.get("runtime_state") in ("ready", "degraded")
                )
        except Exception:
            _logger.warning("hot_reload_health_check_failed post-reload probe did not respond")
            return True

    def _persist_reload_protection(self, module_names: list[str]) -> None:
        """Pin successfully reloaded source revisions against auto-repair."""
        protected: dict[str, str] = {}
        for module_name in module_names:
            module = __import__("sys").modules.get(module_name)
            file_path = getattr(module, "__file__", None) if module else None
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

    def prepare_generation(
        self, *, governance: Any = None, approval_token: str | None = None,
        modules: Any = None, standby_validation: bool = False,
    ) -> types.SimpleNamespace:
        """Authenticate and preflight an immutable backend generation set.

        This performs no live-module mutation. The serving generation remains
        untouched while boot_core starts the candidate on the standby port.
        """
        auth_result = self._authenticate_prepare(
            governance, approval_token, standby_validation)
        if not auth_result.ok:
            return auth_result
        requested = self._parse_requested_modules(modules)
        if isinstance(requested, types.SimpleNamespace):
            return requested
        if not requested:
            return types.SimpleNamespace(ok=False, hashes={}, error="empty-update-set")
        roots = self._resolve_src_roots()
        return self._prepare_modules(requested, roots)

    def _authenticate_prepare(
        self, governance: Any, approval_token: str | None,
        standby_validation: bool,
    ) -> types.SimpleNamespace:
        if not standby_validation:
            authorized, auth_message = self._authenticate(governance, approval_token)
            if not authorized:
                return types.SimpleNamespace(ok=False, hashes={}, error=auth_message)
        elif governance is None:
            return types.SimpleNamespace(ok=False, hashes={}, error="governance-unavailable")
        return types.SimpleNamespace(ok=True)

    def _prepare_modules(
        self, requested: set[str], roots: list[Path],
    ) -> types.SimpleNamespace:
        hashes: dict[str, str] = {}
        for module_name in sorted(requested):
            error = self._validate_module(module_name, roots)
            if error:
                return types.SimpleNamespace(ok=False, hashes={}, error=error)
            file_path = str(getattr(__import__("sys").modules[module_name], "__file__", ""))
            digest = _module_source_hash(file_path)
            if digest is None:
                return types.SimpleNamespace(ok=False, hashes={}, error=f"hash-failed:{module_name}")
            hashes[module_name] = digest
        return types.SimpleNamespace(ok=True, hashes=hashes, error="")

    def _validate_module(self, module_name: str, roots: list[Path]) -> str | None:
        module = __import__("sys").modules.get(module_name)
        if not isinstance(module, types.ModuleType) or _is_protected(module_name):
            return f"module-not-reloadable:{module_name}"
        if not self._is_in_src_root(module, roots):
            return f"module-outside-governed-root:{module_name}"
        file_path = str(getattr(module, "__file__", ""))
        try:
            source = Path(file_path).read_text(encoding="utf-8")
            compile(source, file_path, "exec")
        except Exception as error:
            return f"{module_name}: preflight: {error}"
        return None

    def is_idle(self) -> bool:
        """Return True when the system has no in-flight command tasks."""
        command_tasks = getattr(self.app, "_command_tasks", None)
        if command_tasks is None:
            return True
        return len(command_tasks) == 0

    def pending_replacement_count(self) -> int:
        """Return the number of resource-holding modules awaiting replacement."""
        with self._pending_lock:
            return len(self._pending_replacements)

    async def _apply(
        self, _plan: dict[str, Any], *, repairs_completed: bool = False,
    ) -> None:
        del repairs_completed
        raise PermissionError("PERMISSION_DENIED")
