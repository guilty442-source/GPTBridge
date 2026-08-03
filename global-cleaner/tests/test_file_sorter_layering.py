from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "file-sorter"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "file_sorter"
sys.path.insert(0, str(SERVICES))

from file_sorter.infrastructure.cleanup import VideoFingerprintCache
from file_sorter.infrastructure.sorter_engine import resolve_state_root


def test_file_sorter_has_owned_layers_and_thin_entries() -> None:
    for layer in ("application", "domain", "infrastructure"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]
    assert len((TOOL_ROOT / "src" / "main.py").read_text(encoding="utf-8").splitlines()) <= 10
    channel_source = (TOOL_ROOT / "src" / "channel_runtime.py").read_text(encoding="utf-8")
    assert "GovernedToolRuntime" in channel_source
    assert "websockets.serve" not in channel_source


def test_file_sorter_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_file_sorter_database_defaults_to_tool_folder() -> None:
    cache = VideoFingerprintCache()
    assert cache.database_path.resolve().is_relative_to(TOOL_ROOT.resolve())
    assert cache.database_path.name == "video-fingerprints.sqlite3"


def test_file_sorter_rejects_external_state_root(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="FILE_SORTER_STATE_SCOPE_DENIED"):
        resolve_state_root(tmp_path)
