from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class AutomaticRepairPolicy:
    required_for: tuple[str, ...]
    purpose: str
    boundary: str
    authorization_source: str
    execution_model: str
    unauthorized_repair: bool
    governance_mutation: bool
    authority_expansion: bool
    business_feature_changes: bool
    business_rule_changes: bool
    backup_assistance_allowed: bool
    backup_owner: str
    direct_backup_access: bool
    backup_request_channel: str
    authorization_per_step: bool
    authority_restore_from_backup: bool


@dataclass(frozen=True)
class GovernanceActivationPolicy:
    default_active: bool
    activation_order: str
    direct_load_required: bool
    independent_tool_packaging_exception: str
    packaged_executable_allowed: bool
    execution_access: str
    encapsulation_allowed: bool
    optional: bool
    stop_permission: bool
    disable_permission: bool
    unload_permission: bool
    lifetime: str
    main_system_start_failure: str
    main_system_repair_authority: str
    main_system_repair_scope: str
    repair_completion_gate: str
    failure_behavior: str


@dataclass(frozen=True)
class CodeArchitecturePolicy:
    all_source_code_root: str
    independent_tool_root: str
    independent_tool_folder_outside_project_root: bool
    independent_tool_direct_child_only: bool
    nested_independent_tool_folder: bool
    cross_tool_storage: bool
    independent_tool_root_listing: bool
    implicit_own_root_read: bool
    declared_entry_execution_only: bool
    capability_specific_file_access_only: bool
    source_code_outside_project_root: bool
    source_code_creation_outside_project_root: bool
    source_code_modification_outside_project_root: bool
    source_code_move_outside_project_root: bool
    source_code_update_outside_project_root: bool
    source_execution_target_outside_project_root: bool
    layered_architecture: bool
    modular_architecture: bool
    module_responsibility: str
    cross_layer_access: str
    persistent_state: str
    database_scope: str
    non_database_filesystem_artifact_scope: str
    non_database_filesystem_artifact_owner: str
    permission_model: str
    capability_boundary_model: str
    capability_authority_source: str
    explicit_capability_owner_required: bool
    cross_boundary_capability_access: str
    capability_sharing: bool
    capability_inheritance: bool
    combined_authority: bool
    permission_bypass: bool
    canonical_path_authority: str
    runtime_path_resolution: str
    working_directory_path_authority: bool
    environment_path_override: bool


@dataclass(frozen=True)
class BoundaryEscapePolicy:
    authorization_model: str
    allowlist_dimensions: tuple[str, ...]
    explicit_allow_required: bool
    default_decision: str
    fail_closed: bool
    implicit_permission: bool
    wildcard_permission: bool
    deny_precedence: bool
    prompt_override: bool
    environment_override: bool
    identity_impersonation: bool
    delegated_or_proxy_write: bool
    path_traversal: bool
    symbolic_link_escape: bool
    capability_token_required: bool
    canonical_path_validation: bool
    unknown_actor_access: str
    authorization_failure_code: str
    authorization_failure_localization_key: str
    authorization_failure_message_source: str
    internal_permission_detail_disclosure: bool


@dataclass(frozen=True)
class PermissionDistributionPolicy:
    authority: str
    permission_hierarchy_role: str
    directory_source: str
    decision_model: str
    grant_format: str
    grant_scope: str
    identity_group_requirement: str
    legacy_identity_compatibility: bool
    identity_aliases: bool
    self_grant: bool
    delegation: bool
    inheritance: bool
    privilege_expansion: bool
    enforcement_requirement: str
    main_system_inherent_permission: bool
    independent_tool_inherent_permission: bool
    sole_permission_source: str
    governance_activation_precondition: str
    authority_activation_order: tuple[str, ...]
    all_registered_independent_tools_enabled: bool
    independent_tool_grant_scope: str
    cross_tool_access: bool
    permission_directory_independent_authority: bool


@dataclass(frozen=True)
class IdentityAuthenticationPolicy:
    mechanism: str
    signature_algorithm: str
    issuer: str
    audience: str
    active_identity_group_source: str
    identity_registry_source: str
    permission_registry_source: str
    key_management_source: str
    verification_authority: str
    failure_code: str


@dataclass(frozen=True)
class IdentifierLabelPolicy:
    schema: str
    machine_identifier_language: str
    tool_id_pattern: str
    actor_pattern: str
    capability_pattern: str
    action_pattern: str
    target_pattern: str
    data_scope_pattern: str
    locale_key_pattern: str
    approved_name_source: str
    display_label_source: str
    manifest_literal_display_label: bool
    aliases_allowed: bool
    category_labels_allowed: bool
    case_sensitive: bool
    failure_code: str


@dataclass(frozen=True)
class LocalizationPolicy:
    authority: str
    owner_root_template: str
    plugin_root_template: str
    traditional_chinese_filename: str
    format: str
    encoding: str
    required_key_source: str
    manifest_name_key_field: str
    manifest_window_title_key_field: str
    permission_denied_key: str
    literal_localized_display_text_in_manifest: bool
    localized_text_in_source_code: bool
    shared_cross_tool_locale_plugin: bool
    missing_key_behavior: str
    failure_code: str


@dataclass(frozen=True)
class GovernanceWorkflowPolicy:
    authority_activation_flow: tuple[str, ...]
    privileged_operation_flow: tuple[str, ...]
    code_update_flow: tuple[str, ...]
    authority_release_flow: tuple[str, ...]
    backup_flow: tuple[str, ...]
    automatic_repair_flow: tuple[str, ...]
    shared_layer_write_flow: tuple[str, ...]
    failure_behavior: str
    step_reordering: bool
    step_skipping: bool
    executor_defined_steps: bool


@dataclass(frozen=True)
class IndependentToolSeparationPolicy:
    global_scope_definition: str
    global_cleaner_identity: str
    global_cleaner_authority: tuple[str, ...]
    global_cleaner_prohibited: tuple[str, ...]
    shared_identity: bool
    shared_source_root: bool
    permission_inheritance: bool
    delegated_or_proxy_execution: bool
    owner_execution_request_required: bool
    request_authentication: str
    requester_write_authority: bool
    request_failure_code: str


@dataclass(frozen=True)
class SharedLayerPolicy:
    module_root: str
    jurisdiction: str
    main_system_module_member: bool
    main_system_inherent_access: bool
    independent_module: bool
    source_root: str
    data_root: str
    database_path: str
    ai_database_path: str
    temporary_root: str
    central_index_engine: str
    central_index_schema: str
    central_index_role: str
    original_data_storage: str
    source_write_access: str
    data_read_access: str
    data_write_access: str
    write_authorization: str
    direct_filesystem_write: bool
    executable_content: bool
    runtime_code_loading: bool
    direct_main_to_tool_instruction: bool
    direct_tool_to_tool_instruction: bool
    unchanneled_instruction_decision: str
    automatic_repair_authority: str
    automatic_repair_scope: str
    path_escape: bool
    failure_code: str


@dataclass(frozen=True)
class SystemResponsibilityPolicy:
    git: str
    sql: str
    qdrant_rag: str
    llm: str
    separation: str
    governed_flow: str
    management_owner: str
    management_source_basis: tuple[str, ...]
    llm_inference_as_source_of_truth: bool


@dataclass(frozen=True)
class GovernancePolicy:
    authority_version: int
    authority: str
    permission_hierarchy_role: str
    authority_files: tuple[str, ...]
    top_level_rule: str
    governance_rule_sources: tuple[str, ...]
    governance_rule_count: int
    governance_rule_partitioning: bool
    subordinate_governance_rule_definition: bool
    managed_directory_sources: tuple[str, ...]
    managed_code_rule_directory_sources: tuple[str, ...]
    governance_execution_roots: tuple[str, ...]
    managed_program_authority: str
    storage_model: str
    module_root: str
    implementation: str
    source_code_requirement: str
    localization_requirement: str
    localized_text_in_source: str
    runtime_access: str
    runtime_writers: tuple[str, ...]
    mutation_channels: tuple[str, ...]
    main_system_access: str
    independent_tool_access: str
    hot_update_access: str
    authority_file_protection: str
    authority_integrity_validation: str
    activation: GovernanceActivationPolicy
    code_version_policy_source: str
    authority_overwrite_allowed: bool
    authority_overwrite_prohibited_channels: tuple[str, ...]
    automatic_repair: AutomaticRepairPolicy
    code_architecture: CodeArchitecturePolicy
    boundary_escape: BoundaryEscapePolicy
    permission_distribution: PermissionDistributionPolicy
    identity_authentication: IdentityAuthenticationPolicy
    identifier_labels: IdentifierLabelPolicy
    localization: LocalizationPolicy
    workflow: GovernanceWorkflowPolicy
    independent_tool_separation: IndependentToolSeparationPolicy
    shared_layer: SharedLayerPolicy
    system_responsibilities: SystemResponsibilityPolicy
    replacement_policy: str
    enforcement_role: str


GOVERNANCE_POLICY: Final[GovernancePolicy] = GovernancePolicy(
    authority_version=3,
    authority="governance-rule-only-top-level",
    permission_hierarchy_role="only-top-level-permission-authority",
    authority_files=(
        "governance_rule/governance_policy.py",
        "governance_rule/codex/__init__.py",
        "governance_rule/codex/sovereigns.py",
        "governance_rule/codex/chinese.py",
        "governance_rule/codex/sovereigns_chinese.py",
        "governance_rule/code_rule_directory.py",
        "governance_rule/permission_directory/directory_authority.py",
        "governance_rule/execution/authentication/__init__.py",
        "governance_rule/execution/integrity/__init__.py",
        "governance_rule/execution/versioning/__init__.py",
        "governance_rule/permission_directory/execution/identity_registry/__init__.py",
        "governance_rule/permission_directory/execution/path_guard/__init__.py",
        "main-system/src-ui/main/governance-bootstrap.ts",
        "main-system/src-core/core_system/governance_runtime.py",
    ),
    top_level_rule="governance_policy",
    governance_rule_sources=(
        "governance_rule/governance_policy.py",
    ),
    governance_rule_count=1,
    governance_rule_partitioning=False,
    subordinate_governance_rule_definition=False,
    managed_directory_sources=(
        "governance_rule/permission_directory/directory_authority.py",
    ),
    managed_code_rule_directory_sources=(
        "governance_rule/code_rule_directory.py",
    ),
    governance_execution_roots=(
        "governance_rule/execution",
    ),
    managed_program_authority="none-read-and-execute-only",
    storage_model="independent-read-only-programming-language-database",
    module_root="governance_rule",
    implementation="programming-language-code",
    source_code_requirement="programming-language-only",
    localization_requirement="external-language-plugin-files-only",
    localized_text_in_source="prohibited",
    runtime_access="read-only",
    runtime_writers=(),
    mutation_channels=(),
    main_system_access="read-only",
    independent_tool_access="read-only-snapshot-only",
    hot_update_access="read-only-no-replacement",
    authority_file_protection="operating-system-read-only-and-runtime-write-deny",
    authority_integrity_validation="required-before-load-and-before-enforcement",
    activation=GovernanceActivationPolicy(
        default_active=True,
        activation_order="before-main-system-and-all-other-modules",
        direct_load_required=True,
        independent_tool_packaging_exception="governance_rule-only",
        packaged_executable_allowed=False,
        execution_access="read-only-direct-execution",
        encapsulation_allowed=False,
        optional=False,
        stop_permission=False,
        disable_permission=False,
        unload_permission=False,
        lifetime="entire-host-runtime",
        main_system_start_failure="enter-automatic-repair",
        main_system_repair_authority="governance-policy-only",
        main_system_repair_scope="main-system-stability-only",
        repair_completion_gate="governance-reverify-before-normal-mode",
        failure_behavior="deny-main-system-startup-with-permission-denied",
    ),
    code_version_policy_source="directory-authority-only",
    authority_overwrite_allowed=False,
    authority_overwrite_prohibited_channels=(
        "backup",
        "restore",
        "hot-update",
        "automatic-repair",
        "main-system-runtime",
        "independent-tool-runtime",
    ),
    automatic_repair=AutomaticRepairPolicy(
        required_for=(
            "main-system",
            "each-independent-tool",
            "shared-layer",
        ),
        purpose="restore-system-stability-only",
        boundary="main-system-central-program-with-target-isolated-database-only",
        authorization_source="governance-policy-only",
        execution_model="main-system-executes-central-repair-with-per-target-isolated-database-only",
        unauthorized_repair=False,
        governance_mutation=False,
        authority_expansion=False,
        business_feature_changes=False,
        business_rule_changes=False,
        backup_assistance_allowed=True,
        backup_owner="global-cleaner-only",
        direct_backup_access=False,
        backup_request_channel="governed-shared-layer-request-channel-only",
        authorization_per_step=True,
        authority_restore_from_backup=False,
    ),
    code_architecture=CodeArchitecturePolicy(
        all_source_code_root="E:/GPTBridge",
        independent_tool_root="E:/GPTBridge/{tool_id}",
        independent_tool_folder_outside_project_root=False,
        independent_tool_direct_child_only=True,
        nested_independent_tool_folder=False,
        cross_tool_storage=False,
        independent_tool_root_listing=False,
        implicit_own_root_read=False,
        declared_entry_execution_only=True,
        capability_specific_file_access_only=True,
        source_code_outside_project_root=False,
        source_code_creation_outside_project_root=False,
        source_code_modification_outside_project_root=False,
        source_code_move_outside_project_root=False,
        source_code_update_outside_project_root=False,
        source_execution_target_outside_project_root=False,
        layered_architecture=True,
        modular_architecture=True,
        module_responsibility="single-responsibility",
        cross_layer_access="declared-contracts-only",
        persistent_state="business-settings-and-runtime-state-database-backed",
        database_scope="per-owner-authority-isolated",
        non_database_filesystem_artifact_scope=(
            "external-language-plugins-global-cleaner-backup-only"
        ),
        non_database_filesystem_artifact_owner="declared-owner-only",
        permission_model="separation-of-duties",
        capability_boundary_model="strictly-separated",
        capability_authority_source="directory-authority-only",
        explicit_capability_owner_required=True,
        cross_boundary_capability_access="declared-authenticated-contracts-only",
        capability_sharing=False,
        capability_inheritance=False,
        combined_authority=False,
        permission_bypass=False,
        canonical_path_authority="E:/GPTBridge",
        runtime_path_resolution="authority-path-modules-only",
        working_directory_path_authority=False,
        environment_path_override=False,
    ),
    boundary_escape=BoundaryEscapePolicy(
        authorization_model="explicit-allowlist-only",
        allowlist_dimensions=(
            "actor",
            "capability",
            "action",
            "target",
            "data-scope",
        ),
        explicit_allow_required=True,
        default_decision="deny",
        fail_closed=True,
        implicit_permission=False,
        wildcard_permission=False,
        deny_precedence=True,
        prompt_override=False,
        environment_override=False,
        identity_impersonation=False,
        delegated_or_proxy_write=False,
        path_traversal=False,
        symbolic_link_escape=False,
        capability_token_required=True,
        canonical_path_validation=True,
        unknown_actor_access="denied",
        authorization_failure_code="PERMISSION_DENIED",
        authorization_failure_localization_key="errors.permission_denied",
        authorization_failure_message_source="external-language-plugin",
        internal_permission_detail_disclosure=False,
    ),
    permission_distribution=PermissionDistributionPolicy(
        authority="governance-policy-only",
        permission_hierarchy_role="only-top-level-permission-authority",
        directory_source="directory-authority-read-only",
        decision_model="explicit-allowlist",
        grant_format="scoped-authenticated-capability-token",
        grant_scope="actor-capability-action-target-and-data-scope",
        identity_group_requirement="active-governance-identity-group-only",
        legacy_identity_compatibility=False,
        identity_aliases=False,
        self_grant=False,
        delegation=False,
        inheritance=False,
        privilege_expansion=False,
        enforcement_requirement="every-privileged-operation-must-be-authorized",
        main_system_inherent_permission=False,
        independent_tool_inherent_permission=False,
        sole_permission_source="single-governance-policy-capability-token",
        governance_activation_precondition=(
            "verified-single-governance-rule-required"
        ),
        authority_activation_order=(
            "governance-policy",
            "code-rule-directory",
            "permission-directory",
            "managed-execution-programs",
            "main-system-and-independent-tools",
        ),
        all_registered_independent_tools_enabled=True,
        independent_tool_grant_scope=(
            "own-declared-capability-boundary-plus-governed-shared-layer"
        ),
        cross_tool_access=False,
        permission_directory_independent_authority=False,
    ),
    identity_authentication=IdentityAuthenticationPolicy(
        mechanism="launcher-attested-governance-signed-capability-token",
        signature_algorithm="hmac-sha256",
        issuer="governance-policy",
        audience="gptbridge-runtime",
        active_identity_group_source="directory-authority-only",
        identity_registry_source="directory-authority-immutable-registry-only",
        permission_registry_source="directory-authority-immutable-registry-only",
        key_management_source="directory-authority-immutable-policy-only",
        verification_authority="governance-execution-authenticator-only",
        failure_code="PERMISSION_DENIED",
    ),
    identifier_labels=IdentifierLabelPolicy(
        schema="gptbridge-identifier-label-v1",
        machine_identifier_language="ascii",
        tool_id_pattern=r"^[a-z0-9][a-z0-9_-]*$",
        actor_pattern=(
            r"^governance/(?:main-system|tool/[a-z0-9][a-z0-9_-]*)$"
        ),
        capability_pattern=r"^[a-z][a-z0-9-]*$",
        action_pattern=r"^[a-z][a-z0-9-]*$",
        target_pattern=r"^[a-z][a-z0-9-]*(?::\{?[a-z0-9_-]+\}?)?$",
        data_scope_pattern=(
            r"^(?:none|[a-z][a-z0-9-]*)(?::\{?[a-z0-9_-]+\}?)?$"
        ),
        locale_key_pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
        approved_name_source="code-rule-directory-only",
        display_label_source="owner-external-locale-plugin-only",
        manifest_literal_display_label=False,
        aliases_allowed=False,
        category_labels_allowed=False,
        case_sensitive=True,
        failure_code="PERMISSION_DENIED",
    ),
    localization=LocalizationPolicy(
        authority="governance-policy-schema-owner-content-owner-tool",
        owner_root_template="{owner_root}",
        plugin_root_template="{owner_root}/locales",
        traditional_chinese_filename="zh-TW.json",
        format="json-object-string-values-only",
        encoding="utf-8",
        required_key_source="code-rule-directory-only",
        manifest_name_key_field="name_key",
        manifest_window_title_key_field="window.title_key",
        permission_denied_key="errors.permission_denied",
        literal_localized_display_text_in_manifest=False,
        localized_text_in_source_code=False,
        shared_cross_tool_locale_plugin=False,
        missing_key_behavior="permission-denied",
        failure_code="PERMISSION_DENIED",
    ),
    workflow=GovernanceWorkflowPolicy(
        authority_activation_flow=(
            "directly-load-default-single-governance-policy-before-main-system",
            "verify-governance-policy-version-and-read-only-integrity",
            "load-and-verify-code-rule-directory",
            "load-and-verify-subordinate-permission-directory",
            "load-and-verify-identity-permission-and-capability-registries",
            "enable-governance-managed-execution-programs",
            "enable-registered-identities-with-own-boundaries-only",
            "keep-governance-active-for-entire-host-runtime",
            "deny-activation-on-any-failure-with-permission-denied",
        ),
        privileged_operation_flow=(
            "load-and-verify-governance-policy-database",
            "load-and-verify-managed-directory-authority-database",
            "verify-launcher-signed-process-bound-identity",
            "query-directory-identity-capability-action-target-data-and-path",
            "governance-policy-authorize-explicit-grant",
            "issue-short-lived-single-use-capability-token",
            "managed-execution-module-execute-authorized-operation-only",
            "verify-post-operation-boundary-and-result",
        ),
        code_update_flow=(
            "receive-explicit-target-version",
            "verify-target-version-increments-current-manifest-version",
            "verify-main-system-hot-update-capability",
            "verify-target-tool-manifest-and-canonical-code-path",
            "execute-versioned-code-update-only",
            "verify-code-and-manifest-target-version-match",
            "reject-or-roll-forward-with-newer-version-on-failure",
        ),
        authority_release_flow=(
            "stop-runtime-authority-writers-none",
            "receive-signed-full-release-with-explicit-higher-version",
            "verify-release-signature-and-both-authority-digests",
            "replace-both-authority-databases-as-one-release",
            "set-authority-databases-operating-system-read-only",
            "restart-and-verify-matching-authority-version",
        ),
        backup_flow=(
            "verify-global-cleaner-managed-backup-capability",
            "verify-requester-is-main-system-or-matching-independent-tool",
            "read-owner-source-without-source-write",
            "create-new-owner-scoped-staged-backup",
            "verify-new-backup-integrity",
            "publish-new-backup",
            "delete-excess-backups-for-same-owner-only",
            "verify-each-owner-backup-count-is-at-most-one",
        ),
        automatic_repair_flow=(
            "detect-stability-failure",
            "request-governance-authorized-repair-capability",
            "verify-own-runtime-boundary-and-prohibited-scopes",
            "optionally-request-backup-extraction-through-shared-layer",
            "global-cleaner-authorize-owner-and-verify-backup-integrity",
            "global-cleaner-stage-requested-recovery-data-in-shared-layer-temp",
            "return-extraction-evidence-through-shared-layer",
            "authorize-recovery-data-use-before-application",
            "execute-listed-stability-repair-actions-only",
            "verify-no-governance-authority-code-or-business-rule-change",
        ),
        shared_layer_write_flow=(
            "load-and-verify-single-governance-policy",
            "verify-registered-independent-tool-identity",
            "verify-shared-layer-request-capabilities",
            "verify-write-action-target-data-scope-and-canonical-path",
            "issue-short-lived-single-use-capability-token",
            "shared-layer-module-authenticate-and-consume-token",
            "reject-any-unchanneled-instruction-with-permission-denied",
            "write-shared-layer-request-transaction-only",
            "verify-no-source-code-or-authority-file-write",
            "deny-on-any-failure-with-permission-denied",
        ),
        failure_behavior="fail-closed-with-permission-denied",
        step_reordering=False,
        step_skipping=False,
        executor_defined_steps=False,
    ),
    independent_tool_separation=IndependentToolSeparationPolicy(
        global_scope_definition="E:/GPTBridge-only",
        global_cleaner_identity="governance/tool/global-cleaner",
        global_cleaner_authority=(
            "gptbridge-global-garbage-cleanup",
            "delete-excess-logs",
            "create-verify-retain-per-owner-managed-backups",
            "governed-backup-extraction-staging",
            "gptbridge-read-only-system-health-check",
            "own-business-settings-and-database",
        ),
        global_cleaner_prohibited=(
            "modify-governance-authority",
        ),
        shared_identity=False,
        shared_source_root=False,
        permission_inheritance=False,
        delegated_or_proxy_execution=False,
        owner_execution_request_required=True,
        request_authentication="governance-policy-issued-capability-token-only",
        requester_write_authority=False,
        request_failure_code="PERMISSION_DENIED",
    ),
    shared_layer=SharedLayerPolicy(
        module_root="shared-layer",
        jurisdiction="governance-policy-only",
        main_system_module_member=False,
        main_system_inherent_access=False,
        independent_module=True,
        source_root="shared-layer/src",
        data_root="shared-layer/data",
        database_path="postgresql:gptbridge_transport:system",
        ai_database_path="postgresql:gptbridge_transport:ai",
        temporary_root="global-cleaner/runtime/temp/shared-layer",
        central_index_engine="postgresql",
        central_index_schema="gptbridge_index",
        central_index_role="structured-index-relations-status-and-audit-source-of-truth",
        original_data_storage="owner-module-private-ntfs",
        source_write_access="governance-versioned-release-only",
        data_read_access=(
            "registered-independent-tools-with-explicit-capability-token-only"
        ),
        data_write_access=(
            "registered-independent-tools-with-explicit-capability-token-only"
        ),
        write_authorization=(
            "governance-policy-actor-action-target-data-path-and-token-required"
        ),
        direct_filesystem_write=False,
        executable_content=False,
        runtime_code_loading=False,
        direct_main_to_tool_instruction=False,
        direct_tool_to_tool_instruction=False,
        unchanneled_instruction_decision="PERMISSION_DENIED",
        automatic_repair_authority="governance-policy-only",
        automatic_repair_scope="shared-database-stability-only",
        path_escape=False,
        failure_code="PERMISSION_DENIED",
    ),
    system_responsibilities=SystemResponsibilityPolicy(
        git="system-version-and-development-history",
        sql="structured-mutable-official-data-postgresql",
        qdrant_rag="qdrant-semantic-knowledge-index",
        llm="understanding-reasoning-and-operations",
        separation="git-sql-rag-and-llm-must-not-replace-one-another",
        governed_flow="llm-understands-reasons-and-operates-rag-retrieves-sql-persists-official-data-git-versions-system-changes",
        management_owner="xingcheng-core-orchestrator-under-governance-rule",
        management_source_basis=(
            "governance-rule-for-authority-permissions-and-boundaries",
            "git-for-system-version-and-development-history",
            "sql-for-structured-mutable-official-data",
            "qdrant-semantic-knowledge-index-for-semantic-retrieval-candidates",
        ),
        llm_inference_as_source_of_truth=False,
    ),
    replacement_policy="explicit-versioned-full-release-only",
    enforcement_role="managed-execution-programs-enforce-but-have-no-authority",
)

_SEALED_GOVERNANCE_POLICY: Final[GovernancePolicy] = GOVERNANCE_POLICY

GOVERNANCE_RULE_CATALOG: Final[tuple[str, ...]] = (
    _SEALED_GOVERNANCE_POLICY.top_level_rule,
)
DEFAULT_ACTIVE_GOVERNANCE_RULES: Final[tuple[str, ...]] = (
    _SEALED_GOVERNANCE_POLICY.top_level_rule,
)


def _sealed_policy_reader(sealed_policy: GovernancePolicy):
    def read_policy() -> GovernancePolicy:
        return sealed_policy

    return read_policy


governance_policy_snapshot: Final = _sealed_policy_reader(
    _SEALED_GOVERNANCE_POLICY
)
del _sealed_policy_reader
