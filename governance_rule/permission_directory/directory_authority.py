from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


TOOL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_-]+$")


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


DIRECTORY_BOUNDARIES: Final[tuple[DirectoryBoundary, ...]] = (
    DirectoryBoundary(
        key="main_system",
        role="the only coordinator governed by the single governance rule",
        roots=(
            "main-system",
        ),
        responsibilities=(
            "authenticated_ipc",
            "independent_tool_discovery",
            "independent_tool_start_and_stop",
            "shared_layer_request_submit",
            "hot_update",
            "frontend_backend_connection_stability",
            "central_automatic_repair_request",
        ),
        runtime_writable_roots=("main-system/runtime/state",),
        excluded_roots=("shared-layer",),
    ),
    DirectoryBoundary(
        key="shared_layer",
        role="independent governance-jurisdiction shared data module",
        roots=("shared-layer",),
        responsibilities=(
            "governed_shared_layer_read",
            "governed_shared_layer_write",
            "governed_shared_layer_request_channel",
            "central_automatic_repair_target",
        ),
        runtime_writable_roots=(
            "shared-layer/data",
            "global-cleaner/runtime/temp/shared-layer",
        ),
        excluded_roots=("shared-layer/src",),
    ),
    DirectoryBoundary(
        key="independent_tools",
        role="manifest-discovered standalone applications with isolated business state",
        roots=("{tool_id}",),
        responsibilities=(
            "independent_tool_business_logic",
            "independent_tool_user_settings",
            "independent_tool_business_storage",
            "independent_tool_ephemeral_temporary_storage",
            "independent_tool_automatic_repair",
        ),
        runtime_writable_roots=(
            "{tool_id}/runtime/settings",
            "global-cleaner/runtime/temp/tools/{tool_id}",
            "{tool_id}/data/business",
        ),
    ),
)

PRODUCT_CATEGORIES: Final[tuple[str, ...]] = ()
SETTINGS_AS_PRODUCT_CATEGORY: Final[bool] = False

INDEPENDENT_TOOL_POLICY: Final[IndependentToolPolicy] = IndependentToolPolicy(
    discovery="gptbridge-direct-child-manifest-driven",
    count_limit=None,
    manifest_name="manifest.json",
    root_template="{tool_id}",
    source_code_scope="tool-root-only",
    source_code_outside_tool_root=False,
    shared_source_code_root=False,
    settings_root_template="{tool_id}/runtime/settings",
    temporary_root_template="global-cleaner/runtime/temp/tools/{tool_id}",
    settings_scope="tool-private",
    business_storage_scope="{tool_id}/data/business",
    business_storage_format="database",
    shared_business_settings=False,
    cross_tool_write=False,
    shared_layer_access="explicit-governance-capability-only",
    shared_layer_write_authorization=(
        "single-use-short-lived-governance-capability-token-required"
    ),
)

TOOL_EXECUTION_REQUEST_MODEL: Final[str] = (
    "governance-authenticated-shared-layer-channel-only"
)
GPTBRIDGE_PROJECT_ROOT: Final[str] = "E:/GPTBridge"
ALL_SOURCE_CODE_ROOT: Final[str] = GPTBRIDGE_PROJECT_ROOT
SOURCE_CODE_OUTSIDE_PROJECT_ROOT: Final[bool] = False
INDEPENDENT_TOOL_ROOT_LISTING: Final[bool] = False
INDEPENDENT_TOOL_IMPLICIT_OWN_ROOT_READ: Final[bool] = False
INDEPENDENT_TOOL_DECLARED_ENTRY_EXECUTION_ONLY: Final[bool] = True
REQUESTER_EXECUTION_AUTHORITY: Final[str] = "none"
REQUESTER_WRITE_AUTHORITY: Final[str] = "none"
UNAUTHENTICATED_TOOL_REQUEST: Final[str] = "PERMISSION_DENIED"
MAIN_SYSTEM_INHERENT_PERMISSION: Final[bool] = False
INDEPENDENT_TOOL_INHERENT_PERMISSION: Final[bool] = False
SOLE_PERMISSION_SOURCE: Final[str] = "governance-policy"

IMMUTABLE_AUTHORITY_ROOTS: Final[tuple[str, ...]] = (
    "governance_rule/governance_policy.py",
    "governance_rule/code_rule_directory.py",
    "governance_rule/permission_directory/directory_authority.py",
)

# System Rescue removed; central automatic repair integrated into main-system.
SHARED_LAYER_ACCESS_POLICY: Final[SharedLayerAccessPolicy] = (
    SharedLayerAccessPolicy(
        module_root="shared-layer",
        jurisdiction="governance-policy-only",
        main_system_module_member=False,
        source_root="shared-layer/src",
        data_root="shared-layer/data",
        database_path="postgresql:gptbridge_transport:system",
        ai_database_path="postgresql:gptbridge_transport:ai",
        temporary_root="global-cleaner/runtime/temp/shared-layer",
        central_index_engine="postgresql",
        central_index_schema="gptbridge_index",
        central_index_access="governance-validated-executor-and-read-gateway-only",
        data_actors="all-registered-independent-tool-identities",
        request_submit_actors=(
            "main-system-and-all-registered-independent-tool-identities"
        ),
        request_process_actors="all-registered-independent-tool-identities",
        permitted_actions=(
            "request",
            "cancel-request",
            "consume-response",
            "claim",
            "respond",
        ),
        capability="system-channel-request-submit",
        request_submit_capability="system-channel-request-submit",
        request_process_capability="system-channel-request-process",
        ai_request_submit_capability="ai-channel-request-submit",
        ai_request_process_capability="ai-channel-request-process",
        target="shared-layer-{channel_id}-request:{tool_id}",
        data_scope="shared-layer-{channel_id}-request",
        source_write=False,
        direct_data_write=False,
        token_required=True,
        database_only=True,
        executable_content=False,
        unchanneled_instruction="PERMISSION_DENIED",
        direct_process_instruction=False,
        failure_code="PERMISSION_DENIED",
    )
)

AUTHORITY_VERSION_POLICY: Final[AuthorityVersionPolicy] = AuthorityVersionPolicy(
    current_version=3,
    initial_version=1,
    explicit_target_version_required=True,
    version_increment_required=True,
    same_version_replacement=False,
    implicit_update=False,
    full_release_required=True,
    signed_release_required=True,
    runtime_update=False,
    failure_code="PERMISSION_DENIED",
)

CODE_VERSION_POLICY: Final[CodeVersionPolicy] = CodeVersionPolicy(
    initial_version="1",
    scope="all-source-code",
    explicit_target_version_required=True,
    unversioned_update=False,
    same_version_update=False,
    hot_update_requires_version_change=True,
    version_source="governance-authority-or-tool-manifest",
    failure_code="PERMISSION_DENIED",
)

KEY_MANAGEMENT_POLICY: Final[KeyManagementPolicy] = KeyManagementPolicy(
    management_authority="directory-authority-only",
    key_material_storage="governance-authenticator-protected-memory-only",
    key_material_in_source=False,
    generation="cryptographically-secure-random",
    minimum_key_bytes=32,
    rotation_interval_seconds=300,
    rotation_enforcement="rotation-due-key-cannot-issue-new-token",
    maximum_key_lifetime_seconds=600,
    accepted_key_window="current-and-unexpired-previous-key-only",
    expired_key_access="denied",
    maximum_token_bytes=8192,
    maximum_token_ttl_seconds=300,
    allowed_clock_skew_seconds=30,
    nonce_required=True,
    minimum_nonce_characters=32,
    replay_protection="atomic-single-use-nonce-store-required",
    nonce_store_owner="main-system-governance-authenticator-only",
    nonce_store_engine="sqlite",
    nonce_store_path="main-system/runtime/state/governance_authentication.sqlite3",
    identity_attestation_issuer="main-system-launcher-only",
    identity_attestation_key_source="protected-launcher-memory-only",
    identity_attestation_key_in_source=False,
    identity_attestation_ttl_seconds=30,
    identity_attestation_process_binding=True,
)

MANAGING_AUTHORITY: Final[str] = "governance-policy"
DIRECTORY_STORAGE_MODEL: Final[str] = (
    "independent-read-only-programming-language-database"
)
PERMISSION_DIRECTORY_MODULE_ROOT: Final[str] = (
    "governance_rule.permission_directory"
)
PERMISSION_EXECUTION_ROOTS: Final[tuple[str, ...]] = (
    "governance_rule/permission_directory/execution",
)
PERMISSION_EXECUTION_AUTHORITY: Final[str] = "none-read-and-execute-only"
HOT_UPDATE_AUTHORITY: Final[str] = "main-system-only"
HOT_UPDATE_AUTHORITY_FILE_ACCESS: Final[str] = "read-only-no-replacement"
ACTIVE_IDENTITY_GROUP_ID: Final[str] = "governance-identity-v1"
# Fine-grained identity groups: every registered identity owns exactly one
# dedicated group so an identity from one tool can never be reused to claim
# another tool's permissions (anti-jailbreak isolation).
IDENTITY_GROUP_MAIN_SYSTEM: Final[str] = "identity-group-M00001"
IDENTITY_GROUP_GOVERNANCE_RULE: Final[str] = "identity-group-G00001"
IDENTITY_GROUP_SHARED_LAYER: Final[str] = "identity-group-S00001"
IDENTITY_GROUP_AI_ASSISTANT: Final[str] = "identity-group-A00001"
IDENTITY_GROUP_AI_COLLABORATION: Final[str] = "identity-group-E00001"
IDENTITY_GROUP_STAR_CHAT: Final[str] = "identity-group-D00001"
IDENTITY_GROUP_FILE_SORTER: Final[str] = "identity-group-F00001"
IDENTITY_GROUP_GLOBAL_CLEANER: Final[str] = "identity-group-C00001"
IDENTITY_GROUP_INVESTMENT_MOBILE: Final[str] = "identity-group-I00001"
IDENTITY_GROUP_XINGCHENG: Final[str] = "identity-group-X00001"
IDENTITY_GROUP_VAULTLY: Final[str] = "identity-group-V00001"
IDENTITY_GROUP_SYSTEM_RESCUE: Final[str] = "identity-group-R00001"
ACTIVE_IDENTITY_GROUP_IDS: Final[tuple[str, ...]] = (
    IDENTITY_GROUP_MAIN_SYSTEM,
    IDENTITY_GROUP_GOVERNANCE_RULE,
    IDENTITY_GROUP_SHARED_LAYER,
    IDENTITY_GROUP_AI_ASSISTANT,
    IDENTITY_GROUP_AI_COLLABORATION,
    IDENTITY_GROUP_STAR_CHAT,
    IDENTITY_GROUP_FILE_SORTER,
    IDENTITY_GROUP_GLOBAL_CLEANER,
    IDENTITY_GROUP_INVESTMENT_MOBILE,
    IDENTITY_GROUP_XINGCHENG,
    IDENTITY_GROUP_VAULTLY,
    IDENTITY_GROUP_SYSTEM_RESCUE,
)
IDENTITY_CODE_PATTERN: Final[str] = r"[A-Z][0-9]{5}"
IDENTITY_LANGUAGE_NAME_PATTERN: Final[str] = r"[a-z][a-z0-9_]*"
IDENTITY_GROUP_REGISTRY_PATH: Final[str] = (
    "governance_rule/permission_directory/registries/permissions/identity_groups.py"
)
IDENTITY_PERMISSION_REGISTRY_PATH: Final[str] = (
    "governance_rule/permission_directory/registries/permissions/identity_permissions.py"
)
CAPABILITY_BOUNDARY_REGISTRY_PATH: Final[str] = (
    "governance_rule/permission_directory/registries/permissions/capability_boundaries.py"
)
TOOL_ROUTE_REGISTRY_PATH: Final[str] = (
    "governance_rule/permission_directory/registries/permissions/tool_routes.py"
)
SOURCE_OWNERSHIP_REGISTRY_PATH: Final[str] = (
    "governance_rule/permission_directory/registries/permissions/source_ownership.py"
)
MANAGED_READ_ONLY_REGISTRY_PATHS: Final[tuple[str, ...]] = (
    IDENTITY_GROUP_REGISTRY_PATH,
    IDENTITY_PERMISSION_REGISTRY_PATH,
    CAPABILITY_BOUNDARY_REGISTRY_PATH,
    TOOL_ROUTE_REGISTRY_PATH,
    SOURCE_OWNERSHIP_REGISTRY_PATH,
)
PERMISSION_REGISTRY_ACCESS: Final[str] = "read-only-directory-managed"

AUTHORITY_PRECEDENCE: Final[tuple[str, ...]] = (
    "immutable-authority-files",
    "governed-shared-layer-module",
    "capability-specific-scope",
    "tool-owned-settings-and-data",
    "main-runtime-state",
)

FORBIDDEN_LEGACY_ROOTS: Final[tuple[str, ...]] = (
    "backups",
    "mobile",
    "resources/governance",
    "governance/docs",
    "governance/reports",
    "src-ui/platform-tools",
    "src-ui/renderer/ui/developer-mode",
    "platform_tools",
    "src-core",
    "src-ui",
    "governance",
    "config",
    "launcher",
    "scripts",
    "runtime/governance",
    "runtime/logs",
    "runtime/exports",
)

_SEALED_DIRECTORY_AUTHORITY_SNAPSHOT: Final[DirectoryAuthoritySnapshot] = (
    DirectoryAuthoritySnapshot(
        authority_version_policy=AUTHORITY_VERSION_POLICY,
        code_version_policy=CODE_VERSION_POLICY,
        managing_authority=MANAGING_AUTHORITY,
        permission_hierarchy_role="subordinate-read-only-permission-directory",
        storage_model=DIRECTORY_STORAGE_MODEL,
        module_root=PERMISSION_DIRECTORY_MODULE_ROOT,
        permission_execution_roots=PERMISSION_EXECUTION_ROOTS,
        permission_execution_authority=PERMISSION_EXECUTION_AUTHORITY,
        governance_default_active=True,
        governance_activation_order="before-main-system-and-all-other-modules",
        governance_direct_load=True,
        governance_packaging_exception="governance_rule-only",
        governance_packaged_executable_allowed=False,
        governance_execution_access="read-only-direct-execution",
        governance_encapsulation_allowed=False,
        governance_stop_permission=False,
        governance_disable_permission=False,
        governance_unload_permission=False,
        main_system_start_failure="enter-automatic-repair",
        main_system_repair_authority="governance-policy-only",
        main_system_repair_scope="main-system-stability-only",
        boundaries=DIRECTORY_BOUNDARIES,
        product_categories=PRODUCT_CATEGORIES,
        settings_as_product_category=SETTINGS_AS_PRODUCT_CATEGORY,
        independent_tool_policy=INDEPENDENT_TOOL_POLICY,
        tool_id_pattern=TOOL_ID_PATTERN.pattern,
        immutable_authority_roots=IMMUTABLE_AUTHORITY_ROOTS,
        shared_layer_access_policy=SHARED_LAYER_ACCESS_POLICY,
        hot_update_authority=HOT_UPDATE_AUTHORITY,
        hot_update_authority_file_access=HOT_UPDATE_AUTHORITY_FILE_ACCESS,
        active_identity_group_id=ACTIVE_IDENTITY_GROUP_ID,
        active_identity_group_ids=ACTIVE_IDENTITY_GROUP_IDS,
        identity_group_registry_path=IDENTITY_GROUP_REGISTRY_PATH,
        identity_permission_registry_path=IDENTITY_PERMISSION_REGISTRY_PATH,
        capability_boundary_registry_path=CAPABILITY_BOUNDARY_REGISTRY_PATH,
        managed_read_only_registry_paths=MANAGED_READ_ONLY_REGISTRY_PATHS,
        permission_registry_access=PERMISSION_REGISTRY_ACCESS,
        key_management_policy=KEY_MANAGEMENT_POLICY,
        authority_precedence=AUTHORITY_PRECEDENCE,
        forbidden_legacy_roots=FORBIDDEN_LEGACY_ROOTS,
    )
)


def _sealed_snapshot_reader(sealed_snapshot: DirectoryAuthoritySnapshot):
    def read_snapshot() -> DirectoryAuthoritySnapshot:
        return sealed_snapshot

    return read_snapshot


directory_authority_snapshot: Final = _sealed_snapshot_reader(
    _SEALED_DIRECTORY_AUTHORITY_SNAPSHOT
)
del _sealed_snapshot_reader
