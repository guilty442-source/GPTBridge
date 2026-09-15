"""global-cleaner consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
    # Own tool path last: insert(0) makes it win over other tools' `backend`
    # packages (several tools ship a top-level `backend` package).
    str(_ROOT / "Standalone tools" / "global-cleaner" / "src"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

########################################################################
# source: global-cleaner/tests/test_file_sorter_layering.py
########################################################################
import ast
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = ROOT / "Standalone tools" / "file-sorter"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "file_sorter"

from file_sorter.infrastructure.cleanup import VideoFingerprintCache
from file_sorter.infrastructure.sorter_engine import resolve_state_root


def test_file_sorter_has_owned_layers_and_thin_entries(
    *,
    tool_root: Path = TOOL_ROOT,
    package: Path = PACKAGE,
) -> None:
    for layer in ("application", "domain", "infrastructure"):
        assert (package / layer / "__init__.py").is_file()
    assert [path.name for path in package.glob("*.py")] == ["__init__.py"]
    assert len((tool_root / "src" / "main.py").read_text(encoding="utf-8").splitlines()) <= 10
    channel_source = (tool_root / "src" / "channel_runtime.py").read_text(encoding="utf-8")
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


def test_file_sorter_database_defaults_to_tool_folder(
    *,
    tool_root: Path = TOOL_ROOT,
) -> None:
    cache = VideoFingerprintCache()
    assert cache.database_path.resolve().is_relative_to(tool_root.resolve())
    assert cache.database_path.name == "video-fingerprints.sqlite3"


def test_file_sorter_rejects_external_state_root(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="FILE_SORTER_STATE_SCOPE_DENIED"):
        resolve_state_root(tmp_path)
