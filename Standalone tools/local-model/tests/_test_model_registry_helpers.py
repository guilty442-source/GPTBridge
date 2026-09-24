"""Shared test helpers (split from consolidated suite)."""
from __future__ import annotations

import _xingcheng_test_support as _s  # noqa: F401

from pathlib import Path

from xingcheng.application.service import LocalAiService


def _make_service(tool_root: Path) -> LocalAiService:
    """Create a LocalAiService with the native runtime disabled for tests.

    These tests verify the native generative language model and specialist
    routing behavior without requiring a loaded native checkpoint.
    """
    return LocalAiService(tool_root, enable_transformer=False)
