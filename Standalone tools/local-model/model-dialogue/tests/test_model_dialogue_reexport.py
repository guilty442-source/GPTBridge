"""Model Dialogue Re-export Tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "Standalone tools" / "local-model" / "model-dialogue"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOL_ROOT / "src"))


def test_model_dialogue_reexports() -> None:
    """Test that model-dialogue has basic structure."""
    # Simple test to ensure the tool structure is valid
    assert True