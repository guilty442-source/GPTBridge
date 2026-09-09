from __future__ import annotations

import json
import re
from pathlib import Path

SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
LOCKED_APPLICATION_VERSION = "1.00000"


def application_version(project_root: Path) -> str:
    package_path = Path(project_root) / "package.json"
    try:
        version = str(
            json.loads(package_path.read_text(encoding="utf-8")).get("version") or ""
        ).strip()
    except OSError:
        return LOCKED_APPLICATION_VERSION
    except json.JSONDecodeError:
        version = ""
    if version != LOCKED_APPLICATION_VERSION:
        raise RuntimeError(
            f"GPTBridge product version is locked to {LOCKED_APPLICATION_VERSION}; "
            f"found {version or 'missing'}"
        )
    return LOCKED_APPLICATION_VERSION
