from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

for path in (
    ROOT / "local-model" / "src" / "backend" / "services",
    ROOT / "shared-layer" / "src",
    ROOT,
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))