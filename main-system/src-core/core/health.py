from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Any


_HEALTH_CACHE_TTL_SECONDS: float = 5.0
_health_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _copy_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        **report,
        "checks": dict(report["checks"]),
        "failed_checks": list(report["failed_checks"]),
    }


def check_core_health(project_root: str | Path | None = None) -> dict[str, Any]:
    """Return a compact health report for the local GPTBridge runtime.

    Results are cached for a few seconds: status aggregation chains call
    this function on every health tick from several owners (HTTP status
    requests, the automation coordinator, connection watchdog, boot-core),
    and the repeated ``shutil.which`` + filesystem probes were saturating
    the backend event loop.  A short TTL keeps the report current while
    collapsing duplicated work to at most one run per window.
    """

    root = Path(project_root or Path.cwd()).resolve()
    cache_key = str(root).casefold()
    now = time.monotonic()
    cached = _health_cache.get(cache_key)
    if cached is not None and now - cached[0] < _HEALTH_CACHE_TTL_SECONDS:
        return _copy_report(cached[1])
    checks = {
        "project_root_exists": root.exists(),
        "package_json_exists": (root / "main-system" / "package.json").exists(),
        "main_system_exists": (root / "main-system" / "src-core").exists(),
        "governance_rule_exists": (root / "governance_rule").exists(),
        "permission_directory_exists": (
            root / "governance_rule" / "permission_directory"
        ).exists(),
        "python_available": bool(sys.executable),
        "node_available": shutil.which("node") is not None,
        "npm_available": shutil.which("npm") is not None or shutil.which("npm.cmd") is not None,
    }
    failed = [name for name, ok in checks.items() if not ok]
    report = {
        "ok": not failed,
        "status": "healthy" if not failed else "degraded",
        "project_root": str(root),
        "checks": checks,
        "failed_checks": failed,
    }
    _health_cache[cache_key] = (now, report)
    return _copy_report(report)
