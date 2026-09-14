"""Frozen dataclass definitions for directory authority (A185 split).

All policy dataclasses live here so ``directory_authority.py`` can
focus on the sealed constants and snapshot reader.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DirectoryBoundary:
    key: str
    role: str
    roots: tuple[str, ...]
    responsibilities: tuple[str, ...]
    runtime_writable_roots: tuple[str, ...]
    excluded_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndependentToolPolicy:
    discovery: str
    count_limit: int | None
    manifest_name: str
    root_template: str
    source_code_scope: str
    source_code_outside_tool_root: bool
    shared_source_code_root: bool
    settings_root_template: str
    temporary_root_template: str
    settings_scope: str
    business_storage_scope: str
    business_storage_format: str
    shared_business_settings: bool
    cross_tool_write: bool
    shared_layer_access: str
    shared_layer_write_authorization: str


@dataclass(frozen=True)
class CapabilityGrant:
    action: str
    target: str
    data_scope: str
    version_transition: str = "none"
    path_match: str = "none"
    path_roots: tuple[str, ...] = ()
    excluded_path_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityAuthority:
    capability: str
    owner: str
    execution_scope: str
    data_write_scope: str
    grants: tuple[CapabilityGrant, ...]
    delegation: bool
    inheritance: bool


@dataclass(frozen=True)
class ManifestRequirement:
    field_path: tuple[str, ...]
    expected_value: str


@dataclass(frozen=True)
class ManifestBinding:
    required: bool
    path_template: str
    tool_id_field: str
    maximum_bytes: int
    required_capabilities: tuple[str, ...]
    requirements: tuple[ManifestRequirement, ...]


@dataclass(frozen=True)
class CapabilityIdentity:
    group_id: str
    actor: str
    bound_tool_id: str
    bound_roots: tuple[str, ...]
    manifest_binding: ManifestBinding
    authentication: str
    identity_code: str
    language_name: str
    codename: str


@dataclass(frozen=True)
class IdentityPermissionBinding:
    group_id: str
    actor: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class IdentityGroup:
    group_id: str
    identities: tuple[CapabilityIdentity, ...]
    management_authority: str
    registry_mode: str
    legacy_identity_compatibility: bool
    aliases_allowed: bool
    unknown_identity_access: str
    authentication: str


@dataclass(frozen=True)
class KeyManagementPolicy:
    management_authority: str
    key_material_storage: str
    key_material_in_source: bool
    generation: str
    minimum_key_bytes: int
    rotation_interval_seconds: int
    rotation_enforcement: str
    maximum_key_lifetime_seconds: int
    accepted_key_window: str
    expired_key_access: str
    maximum_token_bytes: int
    maximum_token_ttl_seconds: int
    allowed_clock_skew_seconds: int
    nonce_required: bool
    minimum_nonce_characters: int
    replay_protection: str
    nonce_store_owner: str
    nonce_store_engine: str
    nonce_store_path: str
    identity_attestation_issuer: str
    identity_attestation_key_source: str
    identity_attestation_key_in_source: bool
    identity_attestation_ttl_seconds: int
    identity_attestation_process_binding: bool


@dataclass(frozen=True)
class AuthorityVersionPolicy:
    current_version: int
    initial_version: int
    explicit_target_version_required: bool
    version_increment_required: bool
    same_version_replacement: bool
    implicit_update: bool
    full_release_required: bool
    signed_release_required: bool
    runtime_update: bool
    failure_code: str


@dataclass(frozen=True)
class CodeVersionPolicy:
    initial_version: str
    scope: str
    explicit_target_version_required: bool
    unversioned_update: bool
    same_version_update: bool
    hot_update_requires_version_change: bool
    version_source: str
    failure_code: str


@dataclass(frozen=True)
class SharedLayerAccessPolicy:
    module_root: str
    jurisdiction: str
    main_system_module_member: bool
    source_root: str
    data_root: str
    database_path: str
    ai_database_path: str
    temporary_root: str
    central_index_engine: str
    central_index_schema: str
    central_index_access: str
    data_actors: str
    request_submit_actors: str
    request_process_actors: str
    permitted_actions: tuple[str, ...]
    capability: str
    request_submit_capability: str
    request_process_capability: str
    ai_request_submit_capability: str
    ai_request_process_capability: str
    target: str
    data_scope: str
    source_write: bool
    direct_data_write: bool
    token_required: bool
    database_only: bool
    executable_content: bool
    unchanneled_instruction: str
    direct_process_instruction: bool
    failure_code: str


@dataclass(frozen=True)
class AutomaticRepairBoundary:
    owner: str
    allowed_scope: str
    authorization_source: str
    authorization_failure_code: str
    prohibited_scope: tuple[str, ...]


@dataclass(frozen=True)
class DirectoryAuthoritySnapshot:
    authority_version_policy: AuthorityVersionPolicy
    code_version_policy: CodeVersionPolicy
    managing_authority: str
    permission_hierarchy_role: str
    storage_model: str
    module_root: str
    permission_execution_roots: tuple[str, ...]
    permission_execution_authority: str
    governance_default_active: bool
    governance_activation_order: str
    governance_direct_load: bool
    governance_packaging_exception: str
    governance_packaged_executable_allowed: bool
    governance_execution_access: str
    governance_encapsulation_allowed: bool
    governance_stop_permission: bool
    governance_disable_permission: bool
    governance_unload_permission: bool
    main_system_start_failure: str
    main_system_repair_authority: str
    main_system_repair_scope: str
    boundaries: tuple[DirectoryBoundary, ...]
    product_categories: tuple[str, ...]
    settings_as_product_category: bool
    independent_tool_policy: IndependentToolPolicy
    tool_id_pattern: str
    immutable_authority_roots: tuple[str, ...]
    shared_layer_access_policy: SharedLayerAccessPolicy
    hot_update_authority: str
    hot_update_authority_file_access: str
    active_identity_group_id: str
    active_identity_group_ids: tuple[str, ...]
    identity_group_registry_path: str
    identity_permission_registry_path: str
    capability_boundary_registry_path: str
    managed_read_only_registry_paths: tuple[str, ...]
    permission_registry_access: str
    key_management_policy: KeyManagementPolicy
    authority_precedence: tuple[str, ...]
    forbidden_legacy_roots: tuple[str, ...]


__all__ = [
    "DirectoryBoundary",
    "IndependentToolPolicy",
    "CapabilityGrant",
    "CapabilityAuthority",
    "ManifestRequirement",
    "ManifestBinding",
    "CapabilityIdentity",
    "IdentityPermissionBinding",
    "IdentityGroup",
    "KeyManagementPolicy",
    "AuthorityVersionPolicy",
    "CodeVersionPolicy",
    "SharedLayerAccessPolicy",
    "AutomaticRepairBoundary",
    "DirectoryAuthoritySnapshot",
]
