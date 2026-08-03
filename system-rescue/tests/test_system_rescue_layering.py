from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "system-rescue"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "system_rescue"
sys.path.insert(0, str(SERVICES))

from system_rescue.application.automatic_repair import CentralAutomaticRepairService
from system_rescue.application.service import SystemRescueService


def test_system_rescue_has_owned_layers_and_thin_entries() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == []
    assert len((TOOL_ROOT / "src" / "main.py").read_text(encoding="utf-8").splitlines()) <= 10
    channel = (TOOL_ROOT / "src" / "channel_runtime.py").read_text(encoding="utf-8")
    assert "CentralRepairExecutor" in channel
    assert "backup_extract_paths" not in channel


def test_system_rescue_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_repair_recognizes_both_shared_channel_databases(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tool = project / "system-rescue"
    shared = project / "shared-layer"
    (shared / "src" / "shared_layer").mkdir(parents=True)
    (shared / "src" / "shared_layer" / "store.py").write_text("", encoding="utf-8")
    (shared / "data").mkdir()
    system_database = shared / "data" / "system-channel.sqlite3"
    ai_database = shared / "data" / "ai-channel.sqlite3"
    legacy_database = shared / "data" / "shared-layer.sqlite3"
    for database in (system_database, ai_database, legacy_database):
        database.touch()
    service = CentralAutomaticRepairService(project, tool)
    candidates = set(service._sqlite_candidates(shared))
    assert system_database in candidates
    assert ai_database in candidates
    assert legacy_database not in candidates


def test_repair_deletes_files_but_preserves_directories(tmp_path: Path) -> None:
    project = tmp_path / "project"
    tool = project / "system-rescue"
    nested = tool / "data" / "audit" / "nested"
    nested.mkdir(parents=True)
    garbage = nested / "old.log"
    garbage.write_text("old", encoding="utf-8")
    service = SystemRescueService(project, tool)
    result = service.repair()
    assert result["directories_preserved"] is True
    assert nested.is_dir()
    assert not garbage.exists()
