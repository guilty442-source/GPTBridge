"""Source ownership helpers (A185 split).

Contains helper functions extracted from source_ownership_errors.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Final


def _check_package_layers(
    root: Path,
    package_root: str,
    required_layers: frozenset[str],
    package_label: str,
    errors: list[str],
) -> None:
    """Check that a package has all required layers and no stray sources."""
    package = root / package_root
    for layer in required_layers:
        if not (package / layer / "__init__.py").is_file():
            errors.append(f"{package_label} layer is missing: {layer}")
    for source in package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"{package_label} source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )


def _check_shared_layer_sources(
    root: Path,
    shared_root: Path,
    allowed_sources: frozenset[str],
    allowed_prefixes: tuple[str, ...],
    forbidden_terms: tuple[str, ...],
    errors: list[str],
) -> None:
    """Check shared-layer sources for ownership and forbidden business terms."""
    for source in shared_root.rglob("*.py"):
        relative = source.relative_to(shared_root).as_posix()
        if (
            relative not in allowed_sources
            and not relative.startswith(allowed_prefixes)
        ):
            errors.append(f"unowned shared-layer source: {relative}")
        try:
            content = source.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError) as error:
            errors.append(f"shared-layer source is unreadable: {relative}: {error}")
            continue
        matches = sorted(
            term for term in forbidden_terms if term in content
        )
        if matches:
            errors.append(
                f"shared-layer contains business knowledge: {relative}: {', '.join(matches)}"
            )


def _check_cross_tool_imports(
    root: Path,
    owned_import_prefixes: dict[str, str],
    errors: list[str],
) -> None:
    """Check for forbidden cross-tool internal imports."""
    import_patterns = {
        prefix: re.compile(
            rf"^\s*(?:from|import)\s+{re.escape(prefix)}(?:\.|\s|$)",
            re.MULTILINE,
        )
        for prefix in owned_import_prefixes
    }
    sources_to_scan = list(root.glob("*/src/**/*.py"))
    sources_to_scan.extend(root.glob("Standalone tools/*/src/**/*.py"))
    for source in sources_to_scan:
        relative = source.relative_to(root).as_posix()
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(f"owned import source is unreadable: {relative}: {error}")
            continue
        for import_prefix, owner_root in owned_import_prefixes.items():
            if relative.startswith(f"{owner_root}/"):
                continue
            if import_patterns[import_prefix].search(content):
                errors.append(
                    f"cross-tool internal import is forbidden: {relative}: {import_prefix}"
                )


def _check_ai_assistant_network(
    root: Path,
    assistant_package: Path,
    forbidden_patterns: dict[str, re.Pattern[str]],
    errors: list[str],
) -> None:
    """Check AI assistant sources for forbidden direct network access."""
    for source in assistant_package.rglob("*.py"):
        relative = source.relative_to(root).as_posix()
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(f"AI assistant source is unreadable: {relative}: {error}")
            continue
        matches = sorted(
            name
            for name, pattern in forbidden_patterns.items()
            if pattern.search(content)
        )
        if matches:
            errors.append(
                f"AI assistant direct network access is forbidden: "
                f"{relative}: {', '.join(matches)}"
            )


def _check_global_cleaner_vaultly(
    root: Path,
    cleaner_package: Path,
    errors: list[str],
) -> None:
    """Check global-cleaner sources for vaultly private storage references."""
    cleaner_owned_sources = list(cleaner_package.rglob("*.py"))
    cleaner_rules = root / "Standalone tools/global-cleaner/src/backend/services/project_cleaner/domain/cleanup_rules.json"
    if cleaner_rules.is_file():
        cleaner_owned_sources.append(cleaner_rules)
    for source in cleaner_owned_sources:
        try:
            content = source.read_text(encoding="utf-8").replace("\\", "/").casefold()
        except (OSError, UnicodeError):
            continue
        if "vaultly/data/" in content:
            errors.append(
                "global-cleaner must not address vaultly private storage: "
                f"{source.relative_to(root).as_posix()}"
            )


def _check_main_system_business(
    root: Path,
    forbidden_terms: tuple[str, ...],
    errors: list[str],
) -> None:
    """Check main-system sources for forbidden business terms and legacy sources."""
    main_ipc = root / "main-system/src-core/ipc/server.py"
    try:
        main_ipc_source = main_ipc.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        errors.append(f"main IPC source is unreadable: {error}")
    else:
        for forbidden_symbol in (
            "_investment_watch_result_log_payload",
            "_INVESTMENT_WATCH_LOG_",
        ):
            if forbidden_symbol in main_ipc_source:
                errors.append(
                    f"main system contains tool business summarizer: {forbidden_symbol}"
                )

    legacy_main_business_sources = (
        "main-system/src-core/managers/provider_monitor.py",
        "main-system/scripts/smoke/ai_assistant_visual_smoke.py",
    )
    for relative in legacy_main_business_sources:
        if (root / relative).exists():
            errors.append(f"main system contains tool-owned source: {relative}")
    if not (root / "Standalone tools/ai-assistant/scripts/visual_smoke.py").is_file():
        errors.append("AI assistant visual smoke source is missing from its owner")
    for source in (root / "main-system/src-core").rglob("*.py"):
        relative = source.relative_to(root).as_posix()
        try:
            content = source.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError) as error:
            errors.append(f"main-system source is unreadable: {relative}: {error}")
            continue
        matches = sorted(
            term for term in forbidden_terms if term in content
        )
        if matches:
            errors.append(
                f"main system contains tool business knowledge: "
                f"{relative}: {', '.join(matches)}"
            )


__all__ = [
    "_check_package_layers",
    "_check_shared_layer_sources",
    "_check_cross_tool_imports",
    "_check_ai_assistant_network",
    "_check_global_cleaner_vaultly",
    "_check_main_system_business",
]
