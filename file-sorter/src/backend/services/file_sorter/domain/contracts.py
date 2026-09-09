from __future__ import annotations

from typing import Final


FILE_SORTER_VERSION: Final[str] = "1.00000"
FILE_SORTER_REQUEST_COMMANDS: Final[frozenset[str]] = frozenset(
    {"toolbox_run_tool", "toolbox_request_tool_execution"}
)


__all__ = ["FILE_SORTER_REQUEST_COMMANDS", "FILE_SORTER_VERSION"]
