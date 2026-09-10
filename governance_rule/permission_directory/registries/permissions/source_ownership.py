from __future__ import annotations

from pathlib import Path
import re
from typing import Final


SHARED_LAYER_ROOT: Final[str] = "shared-layer/src/shared_layer"
AI_ASSISTANT_PACKAGE_ROOT: Final[str] = (
    "ai-assistant/src/backend/services/ai_nexus"
)
AI_ASSISTANT_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration"}
)
AI_ASSISTANT_FORBIDDEN_NETWORK_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "aiohttp": re.compile(r"^\s*(?:from|import)\s+aiohttp(?:\.|\s|$)", re.MULTILINE),
    "httpx": re.compile(r"^\s*(?:from|import)\s+httpx(?:\.|\s|$)", re.MULTILINE),
    "requests": re.compile(r"^\s*(?:from|import)\s+requests(?:\.|\s|$)", re.MULTILINE),
    "smtp": re.compile(r"^\s*(?:from|import)\s+smtplib(?:\.|\s|$)", re.MULTILINE),
    "urllib-request": re.compile(r"urllib\.request|\burlopen\s*\(", re.MULTILINE),
}
XINGCHENG_PACKAGE_ROOT: Final[str] = "local-model/src/backend/services/xingcheng"
XINGCHENG_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration"}
)
AI_COLLABORATION_PACKAGE_ROOT: Final[str] = (
    "ai-collaboration/src/backend/services/ai_collaboration"
)
AI_COLLABORATION_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration"}
)
INVESTMENT_MOBILE_PACKAGE_ROOT: Final[str] = (
    "investment-mobile/src/backend/services/investment_mobile"
)
INVESTMENT_MOBILE_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration", "presentation"}
)
FILE_SORTER_PACKAGE_ROOT: Final[str] = (
    "file-sorter/src/backend/services/file_sorter"
)
FILE_SORTER_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure"}
)
GLOBAL_CLEANER_PACKAGE_ROOT: Final[str] = (
    "global-cleaner/src/backend/services/project_cleaner"
)
GLOBAL_CLEANER_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure"}
)
VAULTLY_PACKAGE_ROOT: Final[str] = "vaultly/src/backend/services/vaultly"
VAULTLY_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration"}
)
STAR_CHAT_PACKAGE_ROOT: Final[str] = (
    "local-model/model-dialogue/src/backend/services/star_chat"
)
STAR_CHAT_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application"}
)
SYSTEM_RESCUE_PACKAGE_ROOT: Final[str] = (
    "system-rescue/src/backend/services/system_rescue"
)
SYSTEM_RESCUE_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"integration"}
)
SHARED_LAYER_ALLOWED_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "__init__.py",
        "channel.py",
        "directory_snapshot.py",
        "request_client.py",
        "store.py",
        "locator.py",
        "module_locator_repository.py",
        "resource_identity.py",
        "metadata_contract.py",
        "reconcile.py",
        "embedded_browser_client.py",
        "startup.py",
        "runtime_gateway.py",
        "service_probe.py",
    }
)
SHARED_LAYER_ALLOWED_PREFIXES: Final[tuple[str, ...]] = (
    "database/",
    "local/",
    "registry/",
)
SHARED_LAYER_FORBIDDEN_TERMS: Final[frozenset[str]] = frozenset(
    {
        "ai-assistant",
        "ai-collaboration",
        "chatgpt",
        "claude",
        "deepseek",
        "gemini",
        "google-search",
        "grok",
        "holdings",
        "investment-mobile",
        "perplexity",
        "portfolio",
    }
)
MAIN_SYSTEM_FORBIDDEN_BUSINESS_TERMS: Final[frozenset[str]] = frozenset(
    {
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
)
REQUIRED_OWNED_SOURCES: Final[dict[str, frozenset[str]]] = {
    "governance-rule": frozenset(
        {
            "governance_rule/permission_directory/registries/permissions/tool_routes.py",
            "governance_rule/permission_directory/registries/permissions/source_ownership.py",
        }
    ),
    "ai-collaboration": frozenset(
        {
            "ai-collaboration/src/backend/services/ai_collaboration/integration/provider_gateway.py",
            "ai-collaboration/src/backend/services/ai_collaboration/domain/task_protocol.py",
        }
    ),
    "investment-mobile": frozenset(
        {
            "investment-mobile/src/backend/services/investment_mobile/integration/channel_client.py",
        }
    ),
}
FORBIDDEN_LEGACY_BUSINESS_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "shared-layer/src/shared_layer/ai_channel/__init__.py",
        "shared-layer/src/shared_layer/ai_channel/client.py",
        "shared-layer/src/shared_layer/ai_channel/policy.py",
        "shared-layer/src/shared_layer/ai_channel/protocol.py",
        "shared-layer/src/shared_layer/ai_channel/provider_adapters.py",
        "shared-layer/src/shared_layer/mobile_channel/__init__.py",
        "shared-layer/src/shared_layer/mobile_channel/client.py",
        "shared-layer/src/shared_layer/mobile_channel/policy.py",
    }
)
OWNED_IMPORT_PREFIXES: Final[dict[str, str]] = {
    "ai_collaboration": "ai-collaboration",
    "ai_nexus": "ai-assistant",
    "file_sorter": "file-sorter",
    "investment_mobile": "investment-mobile",
    "xingcheng": "local-model",
    "project_cleaner": "global-cleaner",
    "vaultly": "vaultly",
    "star_chat": "local-model",
    "system_rescue": "system-rescue",
}


def source_ownership_errors(project_root: Path) -> list[str]:
    root = Path(project_root).resolve()
    errors: list[str] = []
    shared_root = root / SHARED_LAYER_ROOT

    for source in shared_root.rglob("*.py"):
        relative = source.relative_to(shared_root).as_posix()
        if (
            relative not in SHARED_LAYER_ALLOWED_SOURCES
            and not relative.startswith(SHARED_LAYER_ALLOWED_PREFIXES)
        ):
            errors.append(f"unowned shared-layer source: {relative}")
        try:
            content = source.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError) as error:
            errors.append(f"shared-layer source is unreadable: {relative}: {error}")
            continue
        matches = sorted(
            term for term in SHARED_LAYER_FORBIDDEN_TERMS if term in content
        )
        if matches:
            errors.append(
                f"shared-layer contains business knowledge: {relative}: {', '.join(matches)}"
            )

    for owner, sources in REQUIRED_OWNED_SOURCES.items():
        for relative in sources:
            if not (root / relative).is_file():
                errors.append(f"owned source is missing: {owner}: {relative}")

    for relative in FORBIDDEN_LEGACY_BUSINESS_SOURCES:
        if (root / relative).exists():
            errors.append(f"legacy business source remains in shared layer: {relative}")

    for import_prefix, owner_root in OWNED_IMPORT_PREFIXES.items():
        pattern = re.compile(
            rf"^\s*(?:from|import)\s+{re.escape(import_prefix)}(?:\.|\s|$)",
            re.MULTILINE,
        )
        for source in root.glob("*/src/**/*.py"):
            relative = source.relative_to(root).as_posix()
            if relative.startswith(f"{owner_root}/"):
                continue
            try:
                content = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                errors.append(f"owned import source is unreadable: {relative}: {error}")
                continue
            if pattern.search(content):
                errors.append(
                    f"cross-tool internal import is forbidden: {relative}: {import_prefix}"
                )

    assistant_package = root / AI_ASSISTANT_PACKAGE_ROOT
    for layer in AI_ASSISTANT_REQUIRED_LAYERS:
        if not (assistant_package / layer / "__init__.py").is_file():
            errors.append(f"AI assistant layer is missing: {layer}")
    for source in assistant_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"AI assistant source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )
    for source in assistant_package.rglob("*.py"):
        relative = source.relative_to(root).as_posix()
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(f"AI assistant source is unreadable: {relative}: {error}")
            continue
        matches = sorted(
            name
            for name, pattern in AI_ASSISTANT_FORBIDDEN_NETWORK_PATTERNS.items()
            if pattern.search(content)
        )
        if matches:
            errors.append(
                f"AI assistant direct network access is forbidden: "
                f"{relative}: {', '.join(matches)}"
            )

    xingcheng_package = root / XINGCHENG_PACKAGE_ROOT
    for layer in XINGCHENG_REQUIRED_LAYERS:
        if not (xingcheng_package / layer / "__init__.py").is_file():
            errors.append(f"xingcheng layer is missing: {layer}")
    for source in xingcheng_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"xingcheng source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )

    collaboration_package = root / AI_COLLABORATION_PACKAGE_ROOT
    for layer in AI_COLLABORATION_REQUIRED_LAYERS:
        if not (collaboration_package / layer / "__init__.py").is_file():
            errors.append(f"ai-collaboration layer is missing: {layer}")
    for source in collaboration_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"ai-collaboration source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )

    mobile_package = root / INVESTMENT_MOBILE_PACKAGE_ROOT
    for layer in INVESTMENT_MOBILE_REQUIRED_LAYERS:
        if not (mobile_package / layer / "__init__.py").is_file():
            errors.append(f"investment-mobile layer is missing: {layer}")
    for source in mobile_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"investment-mobile source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )

    file_sorter_package = root / FILE_SORTER_PACKAGE_ROOT
    for layer in FILE_SORTER_REQUIRED_LAYERS:
        if not (file_sorter_package / layer / "__init__.py").is_file():
            errors.append(f"file-sorter layer is missing: {layer}")
    for source in file_sorter_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"file-sorter source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )
    legacy_file_sorter_sources = (
        "file-sorter/src/cleanup.py",
        "file-sorter/src/sorter_v2.py",
        "file-sorter/src/backend/automation_service.py",
    )
    for relative in legacy_file_sorter_sources:
        if (root / relative).exists():
            errors.append(f"legacy file-sorter source remains: {relative}")

    cleaner_package = root / GLOBAL_CLEANER_PACKAGE_ROOT
    for layer in GLOBAL_CLEANER_REQUIRED_LAYERS:
        if not (cleaner_package / layer / "__init__.py").is_file():
            errors.append(f"global-cleaner layer is missing: {layer}")
    for source in cleaner_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"global-cleaner source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )
    legacy_cleaner_sources = (
        "global-cleaner/src/backend/cleanup_engine.py",
        "global-cleaner/src/backend/business_history.py",
        "global-cleaner/src/backend/cleanup_service.py",
    )
    for relative in legacy_cleaner_sources:
        if (root / relative).exists():
            errors.append(f"legacy global-cleaner source remains: {relative}")

    cleaner_owned_sources = list(cleaner_package.rglob("*.py"))
    cleaner_rules = root / "global-cleaner/src/backend/services/project_cleaner/domain/cleanup_rules.json"
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

    vaultly_package = root / VAULTLY_PACKAGE_ROOT
    for layer in VAULTLY_REQUIRED_LAYERS:
        if not (vaultly_package / layer / "__init__.py").is_file():
            errors.append(f"vaultly layer is missing: {layer}")
    for source in vaultly_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"vaultly source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )

    star_chat_package = root / STAR_CHAT_PACKAGE_ROOT
    for layer in STAR_CHAT_REQUIRED_LAYERS:
        if not (star_chat_package / layer / "__init__.py").is_file():
            errors.append(f"star-chat layer is missing: {layer}")
    for source in star_chat_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"star-chat source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )

    system_rescue_package = root / SYSTEM_RESCUE_PACKAGE_ROOT
    for layer in SYSTEM_RESCUE_REQUIRED_LAYERS:
        if not (system_rescue_package / layer / "__init__.py").is_file():
            errors.append(f"system-rescue layer is missing: {layer}")
    for source in system_rescue_package.glob("*.py"):
        if source.name != "__init__.py":
            errors.append(
                f"system-rescue source is outside an owned layer: "
                f"{source.relative_to(root).as_posix()}"
            )

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
    if not (root / "ai-assistant/scripts/visual_smoke.py").is_file():
        errors.append("AI assistant visual smoke source is missing from its owner")
    for source in (root / "main-system/src-core").rglob("*.py"):
        relative = source.relative_to(root).as_posix()
        try:
            content = source.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError) as error:
            errors.append(f"main-system source is unreadable: {relative}: {error}")
            continue
        matches = sorted(
            term for term in MAIN_SYSTEM_FORBIDDEN_BUSINESS_TERMS if term in content
        )
        if matches:
            errors.append(
                f"main system contains tool business knowledge: "
                f"{relative}: {', '.join(matches)}"
            )

    return errors


__all__ = (
    "AI_COLLABORATION_PACKAGE_ROOT",
    "AI_COLLABORATION_REQUIRED_LAYERS",
    "AI_ASSISTANT_FORBIDDEN_NETWORK_PATTERNS",
    "AI_ASSISTANT_PACKAGE_ROOT",
    "AI_ASSISTANT_REQUIRED_LAYERS",
    "XINGCHENG_PACKAGE_ROOT",
    "XINGCHENG_REQUIRED_LAYERS",
    "MAIN_SYSTEM_FORBIDDEN_BUSINESS_TERMS",
    "INVESTMENT_MOBILE_PACKAGE_ROOT",
    "INVESTMENT_MOBILE_REQUIRED_LAYERS",
    "FILE_SORTER_PACKAGE_ROOT",
    "FILE_SORTER_REQUIRED_LAYERS",
    "GLOBAL_CLEANER_PACKAGE_ROOT",
    "GLOBAL_CLEANER_REQUIRED_LAYERS",
    "VAULTLY_PACKAGE_ROOT",
    "VAULTLY_REQUIRED_LAYERS",
    "STAR_CHAT_PACKAGE_ROOT",
    "STAR_CHAT_REQUIRED_LAYERS",
    "SYSTEM_RESCUE_PACKAGE_ROOT",
    "SYSTEM_RESCUE_REQUIRED_LAYERS",
    "FORBIDDEN_LEGACY_BUSINESS_SOURCES",
    "OWNED_IMPORT_PREFIXES",
    "REQUIRED_OWNED_SOURCES",
    "SHARED_LAYER_ALLOWED_SOURCES",
    "SHARED_LAYER_ALLOWED_PREFIXES",
    "SHARED_LAYER_FORBIDDEN_TERMS",
    "source_ownership_errors",
)
