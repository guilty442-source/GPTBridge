from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
SHARED_SRC = Path(__file__).resolve().parents[2] / "shared-layer" / "src"
sys.path.insert(0, str(SRC_CORE))
sys.path.insert(0, str(SHARED_SRC))

from core_system.sovereign_utils import _iso_now, _suppress


def test_shared_sovereign_helpers() -> None:
    assert datetime.fromisoformat(_iso_now()).tzinfo is not None
    with _suppress(ValueError):
        raise ValueError("expected")


def test_sovereign_helpers_are_not_duplicated() -> None:
    sovereign_root = SRC_CORE / "core_system"
    for path in sovereign_root.glob("*sovereign.py"):
        source = path.read_text(encoding="utf-8")
        assert "def _iso_now" not in source, path.name
        assert "def _suppress" not in source, path.name
        assert "self._iso_now()" not in source, path.name
