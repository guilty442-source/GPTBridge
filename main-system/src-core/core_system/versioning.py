from __future__ import annotations

import json
import re
from pathlib import Path

APPLICATION_VERSION_PATTERN = re.compile(r"^\d+\.\d+$")


def application_version(project_root: Path) -> str:
    package_path = Path(project_root) / "package.json"
    try:
        version = str(
            json.loads(package_path.read_text(encoding="utf-8")).get("version") or ""
        ).strip()
    except OSError as error:
        raise RuntimeError("GPTBridge product version source is unavailable") from error
    except json.JSONDecodeError:
        version = ""
    if APPLICATION_VERSION_PATTERN.fullmatch(version) is None:
        raise RuntimeError(f"GPTBridge product version is invalid: {version or 'missing'}")
    return version
