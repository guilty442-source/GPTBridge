from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any


def check_core_health(project_root: str | Path | None = None) -> dict[str, Any]:
    """Return a compact health report for the local GPTBridge runtime."""

    root = Path(project_root or Path.cwd()).resolve()
    checks = {
        "project_root_exists": root.exists(),
        "package_json_exists": (root / "package.json").exists(),
        "src_core_exists": (root / "src-core").exists(),
        "platform_tools_exists": (root / "platform_tools").exists(),
        "python_available": bool(sys.executable),
        "node_available": shutil.which("node") is not None,
        "npm_available": shutil.which("npm") is not None or shutil.which("npm.cmd") is not None,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return {
        "ok": not failed,
        "status": "healthy" if not failed else "degraded",
        "project_root": str(root),
        "checks": checks,
        "failed_checks": failed,
    }
