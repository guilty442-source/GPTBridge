from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "ai-collaboration"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "ai_collaboration"
sys.path.insert(0, str(SERVICES))

from ai_collaboration.infrastructure.repository import AiCollaborationRepository


def test_ai_collaboration_has_owned_layers() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_ai_collaboration_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_ai_collaboration_database_is_tool_owned(tmp_path: Path) -> None:
    repository = AiCollaborationRepository(tmp_path)
    database = repository.db_path.resolve()
    assert database.is_relative_to(tmp_path.resolve())
    assert database.name == "ai_collaboration.sqlite3"
    assert database.is_file()
