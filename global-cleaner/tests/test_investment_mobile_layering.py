from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "investment-mobile"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "investment_mobile"
repository_path = PACKAGE / "infrastructure" / "repository.py"
repository_spec = importlib.util.spec_from_file_location(
    "investment_mobile_compatibility_repository",
    repository_path,
)
assert repository_spec and repository_spec.loader
repository_module = importlib.util.module_from_spec(repository_spec)
repository_spec.loader.exec_module(repository_module)
InvestmentMobileRepository = repository_module.InvestmentMobileRepository


def test_investment_mobile_has_owned_layers() -> None:
    for layer in (
        "application",
        "domain",
        "infrastructure",
        "integration",
        "presentation",
    ):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_investment_mobile_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_investment_mobile_compatibility_layer_cannot_create_a_database(
    tmp_path: Path,
) -> None:
    repository = InvestmentMobileRepository(tmp_path)
    assert repository.persistence_owner == "ai-assistant"
    assert repository.database_path is None
    assert repository.setting("enabled", "missing") == "false"
    repository.set_setting("enabled", "true")
    assert repository.setting("enabled", "missing") == "true"
    assert not list(tmp_path.rglob("*.sqlite3"))
