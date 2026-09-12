from __future__ import annotations

import json
from pathlib import Path
from typing import Final


def _file_sorter_version() -> str:
    try:
        from core_system.versioning import component_version

        return component_version("file-sorter")
    except Exception:
        pass
    manifest_path = Path(__file__).resolve().parents[5] / "manifest.json"
    try:
        version = str(
            json.loads(manifest_path.read_text("utf-8")).get("version", "")
        ).strip()
        if version:
            return version
    except Exception:
        pass
    return "1.0.0"


FILE_SORTER_VERSION: Final[str] = _file_sorter_version()
FILE_SORTER_REQUEST_COMMANDS: Final[frozenset[str]] = frozenset(
    {"toolbox_run_tool", "toolbox_request_tool_execution"}
)


__all__ = ["FILE_SORTER_REQUEST_COMMANDS", "FILE_SORTER_VERSION"]
