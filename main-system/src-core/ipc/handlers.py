"""IPC Handlers — Thin wrapper delegating to command_router module."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main import GPTBridgeApp

from .command_router import CommandRouter


# Re-export for backwards compatibility
from .command_router.constants import (
    TOOL_LIFECYCLE_HANDLERS,
    MAIN_COMMANDS,
    _pending_action_cardinality,
)

__all__ = [
    "TOOL_LIFECYCLE_HANDLERS",
    "MAIN_COMMANDS",
    "_pending_action_cardinality",
    "CommandRouter",
]