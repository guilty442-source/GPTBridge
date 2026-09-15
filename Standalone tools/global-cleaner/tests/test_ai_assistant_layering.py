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
# source: global-cleaner/tests/test_ai_assistant_layering.py
########################################################################
import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services" / "ai_nexus"


def test_ai_assistant_has_owned_layers() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_ai_assistant_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (ROOT / "Standalone tools" / "ai-assistant" / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_ai_assistant_has_no_direct_network_client() -> None:
    forbidden = {
        "aiohttp": re.compile(r"^\s*(?:from|import)\s+aiohttp(?:\.|\s|$)", re.MULTILINE),
        "httpx": re.compile(r"^\s*(?:from|import)\s+httpx(?:\.|\s|$)", re.MULTILINE),
        "requests": re.compile(r"^\s*(?:from|import)\s+requests(?:\.|\s|$)", re.MULTILINE),
        "smtp": re.compile(r"^\s*(?:from|import)\s+smtplib(?:\.|\s|$)", re.MULTILINE),
        "urllib-request": re.compile(r"urllib\.request|\burlopen\s*\(", re.MULTILINE),
    }
    violations: list[str] = []
    for source in PACKAGE.rglob("*.py"):
        content = source.read_text(encoding="utf-8")
        matches = sorted(name for name, pattern in forbidden.items() if pattern.search(content))
        if matches:
            violations.append(f"{source.relative_to(ROOT)}: {', '.join(matches)}")
    assert violations == []


def test_ai_nexus_is_not_imported_by_other_tools() -> None:
    pattern = re.compile(r"^\s*(?:from|import)\s+ai_nexus(?:\.|\s|$)", re.MULTILINE)
    violations: list[str] = []
    for source in (ROOT / "Standalone tools").glob("*/src/**/*.py"):
        if source.is_relative_to(ROOT / "Standalone tools" / "ai-assistant"):
            continue
        if pattern.search(source.read_text(encoding="utf-8")):
            violations.append(source.relative_to(ROOT).as_posix())
    assert violations == []
