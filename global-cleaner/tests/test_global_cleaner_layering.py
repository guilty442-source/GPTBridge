from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "global-cleaner"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "project_cleaner"
sys.path.insert(0, str(TOOL_ROOT / "src"))

from backend.services.project_cleaner.infrastructure.cleanup_engine import (
    ProjectCleanupService,
)


def test_global_cleaner_has_owned_layers_and_thin_entries() -> None:
    for layer in ("application", "domain", "infrastructure"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]
    assert len((TOOL_ROOT / "src" / "main.py").read_text(encoding="utf-8").splitlines()) <= 10
    assert len((TOOL_ROOT / "src" / "test_runner.py").read_text(encoding="utf-8").splitlines()) <= 10


def test_test_artifacts_are_owned_by_global_cleaner() -> None:
    manifest = json.loads((TOOL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    artifacts = manifest["capabilities"]["global-cleanup"]["test_artifacts"]
    sandbox = (
        TOOL_ROOT
        / "src"
        / "backend"
        / "services"
        / "project_cleaner"
        / "infrastructure"
        / "test_sandbox.py"
    ).read_text(encoding="utf-8")

    assert artifacts["owner"] == "global-cleaner"
    assert artifacts["storage"].startswith(
        "global-cleaner/runtime/temp/development/test-artifacts/"
    )
    assert 'tool_root / "runtime" / "temp" / "development"' in sandbox
    assert 'environment["PYTHONPYCACHEPREFIX"]' in sandbox
    assert 'environment["PYTEST_ADDOPTS"]' in sandbox


def test_global_cleaner_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_global_cleaner_database_is_tool_owned(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "global-cleaner").mkdir(parents=True)
    service = ProjectCleanupService(project)
    assert service.business_history.database_path.resolve().is_relative_to(
        (project / "global-cleaner").resolve()
    )


def test_global_cleaner_history_releases_database_handle(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "global-cleaner").mkdir(parents=True)
    service = ProjectCleanupService(project)

    service._append_history("test", ok=True)
    assert len(service._history_records()) == 1
    database = service.business_history.database_path
    database.unlink()

    assert not database.exists()
