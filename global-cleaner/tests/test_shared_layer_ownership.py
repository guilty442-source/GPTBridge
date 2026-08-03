from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SHARED_PACKAGE = ROOT / "shared-layer" / "src" / "shared_layer"


def test_shared_layer_contains_transport_only() -> None:
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
        "local-ai",
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
