"""Automated backend hot-reload watcher ??facade.

This module provides the HotReloadWatcher class.  Implementation
details live in submodules:

  * :mod:`tasks.hot_reload_watcher_constants` ??constants, ChannelHealth.
  * :mod:`tasks.hot_reload_watcher_health` ??health monitoring mixin.
  * :mod:`tasks.hot_reload_watcher_reload` ??reload request mixin.

Watches the governed backend source root and, once file changes quiet
down, requests a module-scoped hot-reload through the maintenance
sovereign.  Every reload is governance-authorized (A13/E6/E29).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hot_reload_watcher_constants import (
    POLL_INTERVAL_SECONDS,
    QUIET_WINDOW_SECONDS,
    WATCH_ROOTS,
    ChannelHealth,
    _is_protected,
    _EXCLUDED_TOKEN_DIRS,
)
from .hot_reload_watcher_health import HotReloadHealthMixin
from .hot_reload_watcher_reload import HotReloadReloadMixin

_logger = logging.getLogger(__name__)


class HotReloadWatcher(HotReloadReloadMixin, HotReloadHealthMixin):
    """Poll-based watcher that requests governed hot-reload on source edits."""

    def __init__(
        self,
        app: Any,
        *,
        project_root: str | Path | None = None,
    ) -> None:
        self.app = app
        fallback_root = Path(__file__).resolve().parents[3]
        self.project_root = Path(
            project_root or getattr(app, "project_root", fallback_root)
        ).resolve()
        self._roots: list[Path] = []
        self._task: asyncio.Task[Any] | None = None
        self._health_task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._enabled = False
        self._snapshot: dict[str, float] = {}
        self._pending: dict[str, float] = {}
        self._in_flight = False
        self._last_reload_at = 0.0
        self._backoff_until = 0.0
        self._reload_timestamps: list[float] = []
        self._channel_health = ChannelHealth()
        # Adaptive polling: start at base interval, increase when stable
        self._adaptive_poll_interval = POLL_INTERVAL_SECONDS
        self._min_poll_interval = POLL_INTERVAL_SECONDS
        self._max_poll_interval = 60.0  # Max 60 seconds
        self._consecutive_no_changes = 0

    # ??? lifecycle ????????????????????????????????????????????????????

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._resolve_roots()
        if not self._roots:
            self._log({"type": "hot_reload_watcher", "enabled": False,
                       "message": "no-watch-roots"})
            return
        self._snapshot = self._scan()
        self._pending = {}
        self._enabled = True
        self._stop.clear()
        self._adaptive_poll_interval = self._min_poll_interval
        self._consecutive_no_changes = 0
        self._task = asyncio.create_task(
            self._loop(),
            name="main-system-hot-reload-watcher",
        )
        self._health_task = asyncio.create_task(
            self._health_monitor_loop(),
            name="hot-reload-channel-health",
        )
        self._log({"type": "hot_reload_watcher", "enabled": True,
                   "roots": [str(root) for root in self._roots]})

    async def stop(self) -> None:
        """Stop the watcher and cleanup resources gracefully."""
        self._enabled = False
        self._stop.set()

        # Cancel the main loop task
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Cancel the health monitor task
        health_task = self._health_task
        self._health_task = None
        if health_task is not None and not health_task.done():
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass

        # Clear pending state
        self._pending.clear()
        self._snapshot.clear()

        _logger.info("HotReloadWatcher stopped gracefully")

    # ??? observation ??????????????????????????????????????????????????

    def _resolve_roots(self) -> None:
        for relative in WATCH_ROOTS:
            root = (self.project_root / relative).resolve()
            if root.is_dir():
                self._roots.append(root)

    def _scan(self) -> dict[str, float]:
        found: dict[str, float] = {}
        for root in self._roots:
            try:
                for path in root.rglob("*.py"):
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    found[str(path)] = stat.st_mtime
            except OSError:
                continue
        return found

    def _track_changes(self, current: dict[str, float]) -> None:
        previous = self._snapshot
        now = time.monotonic()
        for key, mtime in current.items():
            if key not in previous or abs(previous[key] - mtime) > 0.001:
                self._pending[key] = now
        self._snapshot = current

    def _active_generation_hint(self) -> str:
        """Best-effort current generation label for update evidence."""
        return str(getattr(self.app, "active_generation", "") or "")

    def _loaded_module_names(self, changed_paths: list[str]) -> list[str]:
        targets = {Path(path).resolve() for path in changed_paths}
        names: list[str] = []
        for name, module in list(sys.modules.items()):
            if not isinstance(module, types.ModuleType):
                continue
            if _is_protected(name):
                continue
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                resolved = Path(file_path).resolve()
            except (OSError, ValueError):
                continue
            if resolved in targets:
                names.append(name)
        return sorted(names)

    def _token_resource_path(self, changed_paths: list[str]) -> str | None:
        for path in changed_paths:
            try:
                relative = Path(path).resolve().relative_to(self.project_root)
            except (OSError, ValueError):
                continue
            parts = relative.parts
            if not parts or parts[0] != "main-system":
                continue
            token_path = "/".join(parts)
            if any(
                token_path == root or token_path.startswith(root + "/")
                for root in _EXCLUDED_TOKEN_DIRS
            ):
                continue
            return token_path
        return None

    # ??? main loop ????????????????????????????????????????????????????

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                current = self._scan()
                self._track_changes(current)
                if self._pending:
                    now = time.monotonic()
                    if now - max(self._pending.values()) >= QUIET_WINDOW_SECONDS:
                        if self._enabled:
                            changed = list(self._pending.keys())
                            if await self._maybe_reload(changed):
                                self._pending = {}
                                # Reset adaptive interval on reload
                                self._adaptive_poll_interval = self._min_poll_interval
                                self._consecutive_no_changes = 0
                else:
                    # No pending changes - increase interval when stable
                    self._consecutive_no_changes += 1
                    if self._consecutive_no_changes >= 3:
                        self._adaptive_poll_interval = min(
                            self._adaptive_poll_interval * 1.5,
                            self._max_poll_interval,
                        )
            except Exception as error:
                self._log({"type": "hot_reload_watcher_error",
                           "error": f"{type(error).__name__}: {error}"})
            await asyncio.sleep(self._adaptive_poll_interval)

    def _log(self, payload: dict[str, Any]) -> None:
        try:
            state_path = (
                self.project_root / "main-system" / "runtime" / "state"
                / "hot-reload-watcher.json"
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {**payload, "recorded_at": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, state_path)
        except OSError:
            pass
        try:
            log = getattr(self.app, "_log", None)
            if callable(log):
                log(payload)
                return
        except Exception:
            pass
        try:
            print(payload, flush=True)
        except Exception:
            pass


__all__ = ["HotReloadWatcher", "WATCH_ROOTS"]
