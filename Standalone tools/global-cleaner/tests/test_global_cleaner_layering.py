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
# source: global-cleaner/tests/test_global_cleaner_layering.py
########################################################################
import ast
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = ROOT / "Standalone tools" / "global-cleaner"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "project_cleaner"

from backend.services.project_cleaner.infrastructure.cleanup_engine import (
    ProjectCleanupService,
)


def test_global_cleaner_has_owned_layers_and_thin_entries(
    *,
    tool_root: Path = TOOL_ROOT,
    package: Path = PACKAGE,
) -> None:
    for layer in ("application", "domain", "infrastructure"):
        assert (package / layer / "__init__.py").is_file()
    assert [path.name for path in package.glob("*.py")] == ["__init__.py"]
    assert len((tool_root / "src" / "main.py").read_text(encoding="utf-8").splitlines()) <= 10
    assert len((tool_root / "src" / "test_runner.py").read_text(encoding="utf-8").splitlines()) <= 10


def test_test_artifacts_are_owned_by_global_cleaner(
    *,
    tool_root: Path = TOOL_ROOT,
) -> None:
    manifest = json.loads((tool_root / "manifest.json").read_text(encoding="utf-8"))
    artifacts = manifest["capabilities"]["global-cleanup"]["test_artifacts"]
    sandbox = (
        tool_root
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
