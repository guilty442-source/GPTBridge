"""conftest for xingcheng_tools internal tests.

Ensures shared-layer and xingcheng services are importable before
the xingcheng package __init__ triggers the full import chain.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
_PROJECT_ROOT = Path(__file__).resolve().parents[8]

for path in (
    _ROOT,
    _PROJECT_ROOT / "shared-layer" / "src",
    _PROJECT_ROOT / "local-model" / "src" / "backend" / "services",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
