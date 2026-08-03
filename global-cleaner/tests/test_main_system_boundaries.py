from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "main-system"


def test_main_system_has_no_tool_business_modules() -> None:
    assert not (MAIN / "src-core" / "managers" / "provider_monitor.py").exists()
    assert not (MAIN / "scripts" / "smoke" / "ai_assistant_visual_smoke.py").exists()
    assert (ROOT / "ai-assistant" / "scripts" / "visual_smoke.py").is_file()
    assert not (MAIN / "scripts" / "package_platform_tools.py").exists()
    assert (
        ROOT
        / "system-rescue"
        / "src"
        / "backend"
        / "services"
        / "system_rescue"
        / "integration"
        / "platform_packager.py"
    ).is_file()


def test_main_toolbox_lifecycle_is_modularized() -> None:
    task_root = MAIN / "src-core" / "tasks"
    assert (task_root / "tool_path_resolver.py").is_file()
    assert (task_root / "tool_process_registry.py").is_file()
    toolbox = (task_root / "toolbox_service.py").read_text(encoding="utf-8")
    assert "ToolPathResolver" in toolbox
    assert "GPTBRIDGE_SOURCE_RUNTIME_ENTRY" not in toolbox
    assert "GPTBRIDGE_SOURCE_UI_TOOL_ID_QUERY" not in toolbox


def test_main_core_has_no_provider_or_investment_implementation() -> None:
    forbidden = {
        "ai_nexus",
        "chatgpt",
        "claude",
        "deepseek",
        "dividend",
        "gemini",
        "grok",
        "holdings",
        "investment_mobile",
        "local_ai",
        "perplexity",
        "portfolio",
    }
    violations: list[str] = []
    for source in (MAIN / "src-core").rglob("*.py"):
        content = source.read_text(encoding="utf-8").casefold()
        matches = sorted(term for term in forbidden if term in content)
        if matches:
            violations.append(f"{source.relative_to(ROOT)}: {', '.join(matches)}")
    assert violations == []


def test_capacity_inventory_exposes_shared_layer_and_true_project_total() -> None:
    size_inventory = (
        MAIN / "src-ui" / "main" / "platform-tool-sizes.ts"
    ).read_text(encoding="utf-8")
    main_ipc = (MAIN / "src-ui" / "main" / "index.ts").read_text(
        encoding="utf-8"
    )
    main_ui = (MAIN / "src-ui" / "renderer" / "ui" / "App.tsx").read_text(
        encoding="utf-8"
    )
    cleaner_ui = (
        ROOT / "global-cleaner" / "src" / "ui" / "ProjectCleanerWindowApp.tsx"
    ).read_text(encoding="utf-8")

    assert "getSharedLayerSize" in size_inventory
    assert "shared_layer: sharedLayer" in main_ipc
    assert 'data-testid="shared-layer-folder-size"' in main_ui
    assert "formatProjectSize(analysis.total_bytes)" in cleaner_ui


def test_main_typescript_has_no_tool_business_knowledge() -> None:
    forbidden = {
        "chatgpt",
        "claude",
        "deepseek",
        "dividend",
        "gemini",
        "grok",
        "holdings",
        "perplexity",
        "portfolio",
    }
    violations: list[str] = []
    for source in (MAIN / "src-ui").rglob("*"):
        if source.suffix not in {".ts", ".tsx", ".js", ".cjs"}:
            continue
        content = source.read_text(encoding="utf-8").casefold()
        matches = sorted(term for term in forbidden if term in content)
        if matches:
            violations.append(f"{source.relative_to(ROOT)}: {', '.join(matches)}")
    assert violations == []
