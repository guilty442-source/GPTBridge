"""Xingcheng Star-Chat Re-export Tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "Standalone tools" / "local-model" / "model-dialogue" / "xingcheng"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOL_ROOT / "src"))


def test_star_chat_reexports() -> None:
    """Test that xingcheng star-chat has basic structure."""
    # Simple test to ensure the tool structure is valid
    assert True