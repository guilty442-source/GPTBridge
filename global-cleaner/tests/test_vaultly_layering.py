from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "vaultly"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "vaultly"
sys.path.insert(0, str(SERVICES))

from vaultly.infrastructure.repository import VaultlyRepository


def test_vaultly_has_owned_layers() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_vaultly_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_vaultly_database_is_tool_owned(tmp_path: Path) -> None:
    repository = VaultlyRepository(tmp_path)
    database = repository.db_path.resolve()
    assert database.is_relative_to(tmp_path.resolve())
    assert database.name == "vaultly.sqlite3"
    assert database.is_file()
