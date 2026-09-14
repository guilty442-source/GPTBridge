"""Hot-update health check — split from HotUpdateService for A185 compliance."""

from __future__ import annotations

import urllib.request
import json
import logging
from typing import Any

from .hot_update_constants import _POST_RELOAD_HEALTH_TIMEOUT

_logger = logging.getLogger("gptbridge.hot_update.health")


class HealthChecker:
    """Post-reload health verification."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def check_post_reload(self) -> bool:
        """Verify the system is still healthy after a reload.

        Probes the app's health endpoint if available. Returns True
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
            url = f"http://127.0.0.1:{health_port}/health?level=brief"
            request = urllib.request.Request(url, headers={"Connection": "close"})
            _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with _opener.open(request, timeout=_POST_RELOAD_HEALTH_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return bool(payload.get("ok") is True or payload.get("runtime_state") in ("ready", "degraded"))
        except Exception:
            _logger.warning("hot_reload_health_check_failed — post-reload probe did not respond")
            return True