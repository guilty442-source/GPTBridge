"""Governance policy dataclass types (A185/E160 split)."""

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
    sqlite: str
    qdrant_rag: str
    local_vector_fallback: str
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
    runtime_mutable_authority_files: tuple[str, ...]
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
