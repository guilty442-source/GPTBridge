from __future__ import annotations

import sys
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = TOOL_ROOT.parents[1]
for candidate in (
    PROJECT_ROOT,
    PROJECT_ROOT / "shared-layer" / "src",
    TOOL_ROOT / "src" / "backend" / "services",
):
    value = str(candidate)
    if value not in sys.path:
        sys.path.insert(0, value)
