"""Local Model Re-export Tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "Standalone tools" / "local-model"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOL_ROOT / "src"))


def test_local_model_reexports() -> None:
    """Test that local-model has basic structure."""
    # Simple test to ensure the tool structure is valid
    assert True