from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "ai-assistant" / "src" / "backend" / "services" / "ai_nexus"


def test_ai_assistant_has_owned_layers() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_ai_assistant_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (ROOT / "ai-assistant" / "src").rglob("*.py"):
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
    for source in ROOT.glob("*/src/**/*.py"):
        if source.is_relative_to(ROOT / "ai-assistant"):
            continue
        if pattern.search(source.read_text(encoding="utf-8")):
            violations.append(source.relative_to(ROOT).as_posix())
    assert violations == []
