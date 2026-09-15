from __future__ import annotations

from pathlib import Path
import re
from typing import Final
from .source_ownership_helpers import (
    _check_package_layers,
    _check_shared_layer_sources,
    _check_cross_tool_imports,
    _check_ai_assistant_network,
    _check_global_cleaner_vaultly,
    _check_main_system_business,
)


SHARED_LAYER_ROOT: Final[str] = "shared-layer/src/shared_layer"
AI_ASSISTANT_PACKAGE_ROOT: Final[str] = (
    "Standalone tools/ai-assistant/src/backend/services/ai_nexus"
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
XINGCHENG_PACKAGE_ROOT: Final[str] = "Standalone tools/local-model/src/backend/services/xingcheng"
XINGCHENG_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration"}
)
AI_COLLABORATION_PACKAGE_ROOT: Final[str] = (
    "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration"
)
AI_COLLABORATION_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration"}
)
INVESTMENT_MOBILE_PACKAGE_ROOT: Final[str] = (
    "Standalone tools/investment-mobile/src/backend/services/investment_mobile"
)
INVESTMENT_MOBILE_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration", "presentation"}
)
FILE_SORTER_PACKAGE_ROOT: Final[str] = (
    "Standalone tools/file-sorter/src/backend/services/file_sorter"
)
FILE_SORTER_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure"}
)
GLOBAL_CLEANER_PACKAGE_ROOT: Final[str] = (
    "Standalone tools/global-cleaner/src/backend/services/project_cleaner"
)
GLOBAL_CLEANER_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure"}
)
VAULTLY_PACKAGE_ROOT: Final[str] = "Standalone tools/vaultly/src/backend/services/vaultly"
VAULTLY_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application", "domain", "infrastructure", "integration"}
)
STAR_CHAT_PACKAGE_ROOT: Final[str] = (
    "Standalone tools/local-model/model-dialogue/src/backend/services/star_chat"
)
STAR_CHAT_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"application"}
)
SYSTEM_RESCUE_PACKAGE_ROOT: Final[str] = (
    "Standalone tools/system-rescue/src/backend/services/system_rescue"
)
SYSTEM_RESCUE_REQUIRED_LAYERS: Final[frozenset[str]] = frozenset(
    {"integration"}
)
SHARED_LAYER_ALLOWED_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "__init__.py",
        "cache.py",
        "channel.py",
        "directory_snapshot.py",
        "request_client.py",
        "store.py",
        "store_async.py",
        "store_helpers.py",
        "locator.py",
        "module_locator_repository.py",
        "resource_identity.py",
        "metadata_contract.py",
        "reconcile.py",
        "embedded_browser_client.py",
        "startup.py",
        "runtime_gateway.py",
        "service_probe.py",
        "resilient_store.py",
        "resilient_circuit.py",
        "tool_codenames.py",
        "auto_repair_chain.py",
        "channel_runtime.py",
        "channel_types.py",
        "channel_reconnect.py",
        "transactional_outbox.py",
        "audit_sink.py",
        "contract_resolver.py",
        "envelope.py",
        "gateway_metrics.py",
        "rate_limiter.py",
        "connection_mixin.py",
        "heartbeat_mixin.py",
        "architecture_boundary.py",
        "process_control.py",
    }
)
SHARED_LAYER_ALLOWED_PREFIXES: Final[tuple[str, ...]] = (
    "database/",
    "local/",
    "observability/",
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
            "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration/integration/provider_gateway.py",
            "Standalone tools/ai-collaboration/src/backend/services/ai_collaboration/domain/task_protocol.py",
        }
    ),
    "investment-mobile": frozenset(
        {
            "Standalone tools/investment-mobile/src/backend/services/investment_mobile/integration/channel_client.py",
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
    "ai_collaboration": "Standalone tools/ai-collaboration",
    "ai_nexus": "Standalone tools/ai-assistant",
    "file_sorter": "Standalone tools/file-sorter",
    "investment_mobile": "Standalone tools/investment-mobile",
    "xingcheng": "Standalone tools/local-model",
    "project_cleaner": "Standalone tools/global-cleaner",
    "vaultly": "Standalone tools/vaultly",
    "star_chat": "Standalone tools/local-model",
    "system_rescue": "Standalone tools/system-rescue",
}


def source_ownership_errors(project_root: Path) -> list[str]:
    root = Path(project_root).resolve()
    errors: list[str] = []
    shared_root = root / SHARED_LAYER_ROOT

    _check_shared_layer_sources(
        root, shared_root,
        SHARED_LAYER_ALLOWED_SOURCES,
        SHARED_LAYER_ALLOWED_PREFIXES,
        SHARED_LAYER_FORBIDDEN_TERMS,
        errors,
    )

    for owner, sources in REQUIRED_OWNED_SOURCES.items():
        for relative in sources:
            if not (root / relative).is_file():
                errors.append(f"owned source is missing: {owner}: {relative}")

    for relative in FORBIDDEN_LEGACY_BUSINESS_SOURCES:
        if (root / relative).exists():
            errors.append(f"legacy business source remains in shared layer: {relative}")

    _check_cross_tool_imports(root, OWNED_IMPORT_PREFIXES, errors)

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
    _check_ai_assistant_network(
        root, assistant_package, AI_ASSISTANT_FORBIDDEN_NETWORK_PATTERNS, errors,
    )

    _check_package_layers(
        root, XINGCHENG_PACKAGE_ROOT, XINGCHENG_REQUIRED_LAYERS,
        "xingcheng", errors,
    )
    _check_package_layers(
        root, AI_COLLABORATION_PACKAGE_ROOT, AI_COLLABORATION_REQUIRED_LAYERS,
        "ai-collaboration", errors,
    )
    _check_package_layers(
        root, INVESTMENT_MOBILE_PACKAGE_ROOT, INVESTMENT_MOBILE_REQUIRED_LAYERS,
        "investment-mobile", errors,
    )
    _check_package_layers(
        root, FILE_SORTER_PACKAGE_ROOT, FILE_SORTER_REQUIRED_LAYERS,
        "file-sorter", errors,
    )
    legacy_file_sorter_sources = (
        "Standalone tools/file-sorter/src/cleanup.py",
        "Standalone tools/file-sorter/src/sorter_v2.py",
        "Standalone tools/file-sorter/src/backend/automation_service.py",
    )
    for relative in legacy_file_sorter_sources:
        if (root / relative).exists():
            errors.append(f"legacy file-sorter source remains: {relative}")

    cleaner_package = root / GLOBAL_CLEANER_PACKAGE_ROOT
    _check_package_layers(
        root, GLOBAL_CLEANER_PACKAGE_ROOT, GLOBAL_CLEANER_REQUIRED_LAYERS,
        "global-cleaner", errors,
    )
    legacy_cleaner_sources = (
        "Standalone tools/global-cleaner/src/backend/cleanup_engine.py",
        "Standalone tools/global-cleaner/src/backend/business_history.py",
        "Standalone tools/global-cleaner/src/backend/cleanup_service.py",
    )
    for relative in legacy_cleaner_sources:
        if (root / relative).exists():
            errors.append(f"legacy global-cleaner source remains: {relative}")
    _check_global_cleaner_vaultly(root, cleaner_package, errors)

    _check_package_layers(
        root, VAULTLY_PACKAGE_ROOT, VAULTLY_REQUIRED_LAYERS,
        "vaultly", errors,
    )
    _check_package_layers(
        root, STAR_CHAT_PACKAGE_ROOT, STAR_CHAT_REQUIRED_LAYERS,
        "star-chat", errors,
    )
    _check_package_layers(
        root, SYSTEM_RESCUE_PACKAGE_ROOT, SYSTEM_RESCUE_REQUIRED_LAYERS,
        "system-rescue", errors,
    )

    _check_main_system_business(root, MAIN_SYSTEM_FORBIDDEN_BUSINESS_TERMS, errors)

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
