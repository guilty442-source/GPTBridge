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
# source: global-cleaner/tests/test_shared_layer_ownership.py
########################################################################
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SHARED_PACKAGE = ROOT / "shared-layer" / "src" / "shared_layer"


def test_shared_layer_contains_transport_only() -> None:
    # Shared-layer is the platform transport/identity layer for the star tool
    # (xingcheng/xingcheng). It must not leak code or references from any OTHER
    # business tool or product.
    forbidden_terms = {
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
        "perplexity",
        "google-search",
        "ai-assistant",
        "ai-collaboration",
        "investment-mobile",
        "holdings",
        "portfolio",
    }
    violations: list[str] = []
    for source in SHARED_PACKAGE.rglob("*.py"):
        text = source.read_text(encoding="utf-8").casefold()
        matches = sorted(term for term in forbidden_terms if term in text)
        if matches:
            violations.append(f"{source.relative_to(ROOT)}: {', '.join(matches)}")
    assert violations == []


def test_business_modules_have_explicit_owners() -> None:
    assert (
        ROOT
        / "governance_rule"
        / "permission_directory"
        / "registries"
        / "permissions"
        / "tool_routes.py"
    ).is_file()
    assert (
        ROOT
        / "Standalone tools"
        / "ai-collaboration"
        / "src"
        / "backend"
        / "services"
        / "ai_collaboration"
        / "integration"
        / "provider_gateway.py"
    ).is_file()
    assert (
        ROOT
        / "Standalone tools"
        / "investment-mobile"
        / "src"
        / "backend"
        / "services"
        / "investment_mobile"
        / "integration"
        / "channel_client.py"
    ).is_file()


def test_deprecated_shared_business_packages_have_no_source() -> None:
    for directory_name in ("ai_channel", "mobile_channel"):
        directory = SHARED_PACKAGE / directory_name
        assert list(directory.glob("*.py")) == []
