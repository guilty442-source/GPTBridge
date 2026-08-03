from __future__ import annotations

import sys
from pathlib import Path

import pytest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
FILE_SORTER_SERVICES = (
    WORKSPACE_ROOT / "file-sorter" / "src" / "backend" / "services"
)
sys.path.insert(0, str(FILE_SORTER_SERVICES))


@pytest.fixture
def isolated_sorter_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from file_sorter.infrastructure import sorter_engine

    tool_root = tmp_path / "file-sorter-tool"
    tool_root.mkdir()
    monkeypatch.setattr(sorter_engine, "TOOL_ROOT", tool_root)
    return tool_root / "state"
