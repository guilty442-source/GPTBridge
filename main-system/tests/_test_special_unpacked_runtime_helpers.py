"""Shared test helpers (split from consolidated suite)."""
from __future__ import annotations

import _main_system_test_support as _s  # noqa: F401
from _main_system_test_support import ROOT, _read_text_cached, _parse_python_cached

import asyncio
import json
import sys
from pathlib import Path
import pytest
from tasks.toolbox_service import ToolboxService  # noqa: E402


class GovernanceStub:
    def __init__(self) -> None:
        self.authorized_lifecycle: list[tuple[str, str]] = []
        self.bootstrap_tool_ids: list[str] = []

    def authorize_tool_lifecycle(self, _tool_id: str, _action: str) -> None:
        self.authorized_lifecycle.append((_tool_id, _action))
        return None

    def create_tool_governance_bootstrap(self, _tool_id: str) -> str:
        self.bootstrap_tool_ids.append(_tool_id)
        return "governed-bootstrap"

    def can_start_tool(self, _tool_id: str) -> bool:
        return True
