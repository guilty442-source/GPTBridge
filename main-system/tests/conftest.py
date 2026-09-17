from __future__ import annotations

import sys
from pathlib import Path

import pytest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
MAIN_SYSTEM_ROOT = WORKSPACE_ROOT / "main-system"
SRC_CORE = MAIN_SYSTEM_ROOT / "src-core"
SHARED_SRC = WORKSPACE_ROOT / "shared-layer" / "src"
GOVERNANCE_RULE = WORKSPACE_ROOT / "governance_rule"
FILE_SORTER_SERVICES = (
    WORKSPACE_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"
)

# Add all required paths for governance and shared-layer imports
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(MAIN_SYSTEM_ROOT))
sys.path.insert(0, str(SRC_CORE))
sys.path.insert(0, str(SHARED_SRC))
sys.path.insert(0, str(GOVERNANCE_RULE))
sys.path.insert(0, str(FILE_SORTER_SERVICES))


@pytest.fixture
def isolated_sorter_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from file_sorter.infrastructure import sorter_engine

    tool_root = tmp_path / "file-sorter-tool"
    tool_root.mkdir()
    monkeypatch.setattr(sorter_engine, "TOOL_ROOT", tool_root)
    return tool_root / "state"
