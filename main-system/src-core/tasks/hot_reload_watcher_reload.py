"""Hot-reload watcher reload request mixin — refactored for A430 compliance.

Provides the _maybe_reload method and decision-sovereign feedback
loop for the HotReloadWatcher class.
"""

from __future__ import annotations

import time
from typing import Any

from .hot_reload_watcher_constants import (
    MIN_RELOAD_INTERVAL_SECONDS,
    MAX_RELOADS_PER_MINUTE,
    FAILURE_BACKOFF_SECONDS,
)

from .hot_reload_watcher_preflight import PreflightConfirmationMixin
from .hot_reload_watcher_reload_exec import ReloadExecutionMixin


class HotReloadReloadMixin(
    PreflightConfirmationMixin,
    ReloadExecutionMixin,
):
    """Reload request and decision-sovereign feedback methods for HotReloadWatcher."""

    async def _maybe_reload(
        self,
        changed_paths: list[str],
        *,
        user_confirmed: bool = False,
    ) -> bool:
        """Attempt one reload; return whether the pending revision was consumed."""
        if not self._preflight_checks():
            return False

        module_names = self._loaded_module_names(changed_paths)
        if not module_names:
            return True

        if not await self._handle_user_confirmation(module_names, changed_paths, user_confirmed):
            return True

        token_path = self._token_resource_path(changed_paths)
        if token_path is None:
            return True

        return await self._execute_reload(module_names, token_path)