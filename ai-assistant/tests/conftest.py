from __future__ import annotations

import sys
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
SERVICES_ROOT = TOOL_ROOT / "src" / "backend" / "services"
SRC_ROOT = TOOL_ROOT / "src"

for path in (SERVICES_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
