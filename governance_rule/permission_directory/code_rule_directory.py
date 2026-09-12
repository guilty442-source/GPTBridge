from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class CodeRuleDirectorySnapshot:
    authority_version: int
    managing_authority: str
    governing_source: str
    independent_authority: bool
    runtime_write_allowed: bool
    canonical_project_root: str
    main_system_root: str
    shared_layer_root: str
    governance_root: str
    permission_directory_root: str
    independent_tool_root_template: str
    governance_execution_root: str
    permission_execution_root: str
    path_resolution: str
    source_language: str
    localization_source: str
    initial_code_version: str
    implicit_version_change: bool
    identifier_label_schema: str
    approved_tool_ids: tuple[str, ...]
    approved_actor_names: tuple[str, ...]
    approved_capability_names: tuple[str, ...]
    approved_action_names: tuple[str, ...]
    approved_target_names: tuple[str, ...]
    approved_data_scope_names: tuple[str, ...]
    required_locale_keys: tuple[str, ...]
    category_labels: bool
    requirements: tuple[str, ...]


CODE_RULE_DIRECTORY: Final[CodeRuleDirectorySnapshot] = (
    CodeRuleDirectorySnapshot(
        authority_version=1,
        managing_authority="governance-codex-via-enforcement-policy",
        governing_source="governance_rule/codex/__init__.py",
        independent_authority=False,
        runtime_write_allowed=False,
        canonical_project_root="E:/GPTBridge",
        main_system_root="main-system",
        shared_layer_root="shared-layer",
        governance_root="governance_rule",
        permission_directory_root=(
            "governance_rule/permission_directory"
        ),
        independent_tool_root_template="{tool_id}",
        governance_execution_root="governance_rule/execution",
        permission_execution_root=(
            "governance_rule/permission_directory/execution"
        ),
        path_resolution="canonical-authority-path-module-only",
        source_language="programming-language-only",
        localization_source="external-language-plugin-only",
        initial_code_version="1.00000",
        implicit_version_change=False,
        identifier_label_schema="gptbridge-identifier-label-v1",
        approved_tool_ids=(
            "ai-assistant",
            "ai-collaboration",
            "file-sorter",
            "global-cleaner",
            "governance_rule",
            "investment-mobile",
            "xingcheng",
            "shared-layer",
            "system-rescue",
            "vaultly",
        ),
        approved_actor_names=(
            "governance/main-system",
            "governance/tool/ai-assistant",
            "governance/tool/ai-collaboration",
            "governance/tool/file-sorter",
            "governance/tool/global-cleaner",
            "governance/tool/governance_rule",
            "governance/tool/investment-mobile",
            "governance/tool/xingcheng",
            "governance/tool/shared-layer",
            "governance/tool/system-rescue",
            "governance/tool/vaultly",
        ),
        approved_capability_names=(
            "ai-channel-top-level",
            "ai-connections",
            "authenticated-ipc",
            "external-ai",
            "frontend-backend-connection-stability",
            "global-cleanup",
            "managed-backup",
            "governance",
            "governance-authority-read-execute",
            "hot-update",
            "independent-tool-business-logic",
            "independent-tool-business-storage",
            "independent-tool-discovery",
            "independent-tool-start-and-stop",
            "independent-tool-user-settings",
            "investment-analysis",
            "investment-manager",
            "investment-market-search",
            "xingcheng",
            "local-model-platform-execution",
            "permission-directory-read-execute",
            "star-investment-manager-database-read",
            "star-decision",
            "star-global-data-read",
            "star-internal-data-read-write",
            "star-project-database-operations",
            "star-project-programming",
            "system-channel-request-process",
            "system-channel-request-submit",
            "ai-channel-request-process",
            "ai-channel-request-submit",
            "system-health-check",
            "central-automatic-repair",
            "upgrade-optimization",
            "xingcheng-governance-source-read",
            "xingcheng-fault-analysis-read",
        ),
        approved_action_names=(
            "append",
            "approve",
            "authorize",
            "cancel-request",
            "claim",
            "check",
            "connect",
            "create-backup",
            "delete",
            "deny",
            "delete-excess",
            "diagnose",
            "disconnect",
            "discover",
            "enforce",
            "execute",
            "extract-backup",
            "health-check",
            "read",
            "request-read",
            "request-reassessment",
            "consume-response",
            "read-source",
            "receive-external-response",
            "repair",
            "request",
            "request-external-collaboration",
            "respond",
            "rollback",
            "stabilize",
            "start",
            "stop",
            "update",
            "verify",
            "write",
        ),
        approved_target_names=(
            "authenticated-ipc",
            "authority-files",
            "ai-channel:xingcheng",
            "capability-request",
            "central-index",
            "frontend-backend-connection",
            "global-cleanup-candidates",
            "global-source",
            "global-system-health",
            "governance-authority-snapshot",
            "governed-operation",
            "governed-decision",
            "independent-tool-manifests",
            "permission-directory-module",
            "permission-directory-snapshot",
            "non-governance-project-source",
            "non-governance-project-database",
            "owner-resource-resolve:{tool_id}",
            "shared-layer-system-request:{tool_id}",
            "shared-layer-ai-request:{tool_id}",
            "managed-backup-root",
            "local-model-runtime",
            "automatic-repair-database:{tool_id}",
            "backup-owner-source",
            "backup-extract-staging",
            "star-internal-data",
            "tool-business-storage:{tool_id}",
            "tool-business-storage:ai-assistant",
            "tool-code:{tool_id}",
            "tool-process:{tool_id}",
            "tool-runtime:governance_rule",
            "tool-runtime:{tool_id}",
            "tool-settings:{tool_id}",
            "validated-global-garbage",
        ),
        approved_data_scope_names=(
            "connection-metadata",
            "global-read-only",
            "main-runtime-state",
            "xingcheng-internal-data",
            "none",
            "owner-scoped-backup",
            "backup-extract",
            "ai-assistant-investment-database",
            "shared-layer-system-request",
            "shared-layer-ai-request",
            "non-governance-project-source",
            "non-governance-project-database",
            "opaque-locator-id-only",
            "opaque-resource-index",
            "tool-business:{tool_id}",
            "tool-code",
            "tool-settings:{tool_id}",
            "tool-stability:{tool_id}",
            "automatic-repair-knowledge",
            "automatic-repair-runs",
            "crash-diagnosis-records",
            "repair-requests",
            "system-health-snapshot",
            "tool-crash-quarantine",
            "audit-records",
            "runtime-logs",
        ),
        required_locale_keys=(
            "tool.name",
            "tool.window_title",
            "tool.description",
            "errors.permission_denied",
        ),
        category_labels=False,
        requirements=(
            "codex-v1.32010-is-sole-rule-source",
            "all-source-code-within-canonical-project-root",
            "main-system-code-within-main-system-root-only",
            "independent-tool-code-within-own-direct-root-only",
            "no-nested-shared-independent-tool-parent",
            "no-cross-tool-source-or-storage-access-outside-governed-shared-layer",
            "shared-layer-is-an-independent-governance-jurisdiction-module",
            "shared-layer-persists-central-index-relations-status-audit-and-transit-requests",
            "postgresql-is-central-structured-index-source-of-truth",
            "module-original-data-remains-in-owner-private-storage",
            "central-index-physical-path-storage-and-disclosure-prohibited",
            "opaque-locator-resolved-only-after-governance-by-owning-module-or-shared-layer",
            "cross-module-data-access-default-deny-at-governance-shared-layer-and-postgresql",
            "qdrant-semantic-index-is-vector-retrieval-only-with-governed-module-label-filters",
            "star-global-read-highest-decision-no-external-execution",
            "star-internal-data-read-write-permission-files-read-only",
            "shared-layer-source-write-and-executable-content-prohibited",
            "unchanneled-main-to-tool-and-tool-to-tool-instructions-denied",
            "layered-and-modular-single-responsibility-code",
            "database-backed-owner-isolated-persistent-state",
            "permission-and-capability-boundaries-separated",
            "governance-activation-before-permission-distribution",
            "governance-default-direct-load-before-main-system",
            "governance-rule-is-only-independent-tool-packaging-exception",
            "governance-rule-read-only-direct-execution-without-executable-package",
            "governance-encapsulation-stop-disable-and-unload-prohibited",
            "governance-authorized-stability-repair-only",
            "automatic-repair-backup-assistance-via-governed-channel-only",
            "automatic-repair-backup-request-read-return-apply-verify-each-authorized",
            "automatic-repair-has-no-direct-backup-storage-access",
            "explicit-higher-version-required-for-code-update",
            "all-identifiers-and-label-keys-match-governance-schema",
            "approved-identifiers-listed-in-code-rule-directory-only",
            "display-labels-loaded-from-owner-locale-plugin-only",
            "category-labels-and-identifier-aliases-prohibited",
        ),
    )
)


def _sealed_reader(snapshot: CodeRuleDirectorySnapshot):
    def read_snapshot() -> CodeRuleDirectorySnapshot:
        return snapshot

    return read_snapshot


code_rule_directory_snapshot: Final = _sealed_reader(CODE_RULE_DIRECTORY)
del _sealed_reader
