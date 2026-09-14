"""Command Router — Hot-Reload Handler."""

from __future__ import annotations

import sys
from typing import Any, Dict


class HotReloadHandler:
    """Handle app:hot-reload-backend command (A330)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        watcher = getattr(self.app, "hot_reload_watcher", None)
        if watcher is None:
            return "app:hot-reload-backend_result", {
                "ok": False,
                "duty": "release-update-sync",
                "error_code": "HOT_RELOAD_WATCHER_UNAVAILABLE",
                "message": "PERMISSION_DENIED",
            }
        modules = payload.get("modules")
        if modules is not None and (
            isinstance(modules, (str, bytes))
            or not isinstance(modules, (list, tuple))
        ):
            return "app:hot-reload-backend_result", {
                "ok": False,
                "error_code": "INVALID_MODULES",
                "message": "modules must be a list of dotted module names",
            }
        changed_paths = []
        for name in (modules or ()):
            module = sys.modules.get(str(name))
            file_path = getattr(module, "__file__", None)
            if file_path:
                changed_paths.append(str(file_path))
        if not changed_paths:
            return "app:hot-reload-backend_result", {
                "ok": False,
                "error_code": "NO_LOADED_MODULES",
                "message": "no loaded backend modules were selected",
            }
        try:
            accepted = await watcher._maybe_reload(changed_paths)
        except Exception as error:
            return "app:hot-reload-backend_result", {
                "ok": False,
                "error_code": "HOT_RELOAD_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }
        return "app:hot-reload-backend_result", {
            "ok": bool(accepted),
            "duty": "release-update-sync",
            "handover": "prepared" if accepted else "deferred",
        }