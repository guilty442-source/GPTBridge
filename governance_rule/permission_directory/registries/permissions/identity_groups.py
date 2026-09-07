from __future__ import annotations

from typing import Final

from governance_rule.permission_directory.directory_authority import (
    ACTIVE_IDENTITY_GROUP_ID,
    IDENTITY_GROUP_AI_ASSISTANT,
    IDENTITY_GROUP_AI_COLLABORATION,
    IDENTITY_GROUP_FILE_SORTER,
    IDENTITY_GROUP_GLOBAL_CLEANER,
    IDENTITY_GROUP_GOVERNANCE_RULE,
    IDENTITY_GROUP_INVESTMENT_MOBILE,
    IDENTITY_GROUP_MAIN_SYSTEM,
    IDENTITY_GROUP_SHARED_LAYER,
    IDENTITY_GROUP_STAR_CHAT,
    IDENTITY_GROUP_SYSTEM_RESCUE,
    IDENTITY_GROUP_VAULTLY,
    IDENTITY_GROUP_XINGCHENG,
    CapabilityIdentity,
    IdentityGroup,
    ManifestBinding,
    ManifestRequirement,
)


MAIN_SYSTEM_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=IDENTITY_GROUP_MAIN_SYSTEM,
    actor="governance/main-system",
    bound_tool_id="main-system",
    bound_roots=(
        "main-system",
    ),
    manifest_binding=ManifestBinding(
        required=False,
        path_template="",
        tool_id_field="",
        maximum_bytes=0,
        required_capabilities=(),
        requirements=(),
    ),
    authentication="governance-policy-issued-capability-token",
    identity_code="M00001",
    language_name="main_system",
    codename="CENTRAL",
)

GOVERNANCE_RULE_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=IDENTITY_GROUP_GOVERNANCE_RULE,
    actor="governance/tool/governance_rule",
    bound_tool_id="governance_rule",
    bound_roots=("governance_rule",),
    manifest_binding=ManifestBinding(
        required=True,
        path_template="governance_rule/manifest.json",
        tool_id_field="id",
        maximum_bytes=1_048_576,
        required_capabilities=(
            "governance-authority-read-execute",
            "permission-directory-read-execute",
        ),
        requirements=(
            ManifestRequirement(("permissions", "code_scope"), "tool-root-only"),
            ManifestRequirement(("permissions", "database_scope"), "none"),
        ),
    ),
    authentication="governance-policy-issued-capability-token",
    identity_code="G00001",
    language_name="governance_rule",
    codename="SOVEREIGN",
)

SHARED_LAYER_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=IDENTITY_GROUP_SHARED_LAYER,
    actor="governance/tool/shared-layer",
    bound_tool_id="shared-layer",
    bound_roots=("shared-layer",),
    manifest_binding=ManifestBinding(
        required=True,
        path_template="shared-layer/manifest.json",
        tool_id_field="id",
        maximum_bytes=1_048_576,
        required_capabilities=("authenticated-ipc",),
        requirements=(
            ManifestRequirement(("permissions", "code_scope"), "tool-root-only"),
            ManifestRequirement(
                ("permissions", "database_scope"),
                "tool-database-only",
            ),
        ),
    ),
    authentication="governance-policy-issued-capability-token",
    identity_code="S00001",
    language_name="shared_layer",
    codename="CONDUIT",
)

GLOBAL_CLEANER_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=IDENTITY_GROUP_GLOBAL_CLEANER,
    actor="governance/tool/global-cleaner",
    bound_tool_id="global-cleaner",
    bound_roots=("global-cleaner",),
    manifest_binding=ManifestBinding(
        required=True,
        path_template="global-cleaner/manifest.json",
        tool_id_field="id",
        maximum_bytes=1_048_576,
        required_capabilities=(
            "global-cleanup",
            "managed-backup",
            "system-health-check",
        ),
        requirements=(
            ManifestRequirement(
                ("permissions", "code_scope"),
                "tool-root-only",
            ),
            ManifestRequirement(
                ("permissions", "database_scope"),
                "tool-database-only",
            ),
        ),
    ),
    authentication="governance-policy-issued-capability-token",
    identity_code="C00001",
    language_name="global_cleaner",
    codename="SWEEPER",
)


def _business_tool_identity(
    tool_id: str,
    *,
    group_id: str,
    identity_code: str,
    language_name: str,
    codename: str,
    physical_root: str | None = None,
    code_scope: str = "tool-root-only",
    database_scope: str = "tool-database-only",
) -> CapabilityIdentity:
    owner_root = str(physical_root or tool_id).strip()
    return CapabilityIdentity(
        group_id=group_id,
        actor=f"governance/tool/{tool_id}",
        bound_tool_id=tool_id,
        bound_roots=(owner_root,),
        manifest_binding=ManifestBinding(
            required=True,
            path_template=f"{owner_root}/manifest.json",
            tool_id_field="id",
            maximum_bytes=1_048_576,
            required_capabilities=(),
            requirements=(
                ManifestRequirement(
                    ("permissions", "code_scope"),
                    code_scope,
                ),
                ManifestRequirement(
                    ("permissions", "database_scope"),
                    database_scope,
                ),
            ),
        ),
        authentication="governance-policy-issued-capability-token",
        identity_code=identity_code,
        language_name=language_name,
        codename=codename,
    )


AI_ASSISTANT_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "ai-assistant",
    group_id=IDENTITY_GROUP_AI_ASSISTANT,
    identity_code="A00001",
    language_name="ai_assistant",
    codename="STEWARD",
)
AI_COLLABORATION_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "ai-collaboration",
    group_id=IDENTITY_GROUP_AI_COLLABORATION,
    identity_code="E00001",
    language_name="ai_collaboration",
    codename="ENVOY",
)
STAR_CHAT_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "star-chat",
    group_id=IDENTITY_GROUP_STAR_CHAT,
    identity_code="D00001",
    language_name="star_chat",
    codename="DIALOGUE",
    physical_root="local-model/model-dialogue",
    database_scope=(
        "all-project-databases-via-xingcheng-excluding-governance-rule"
    ),
)
FILE_SORTER_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "file-sorter",
    group_id=IDENTITY_GROUP_FILE_SORTER,
    identity_code="F00001",
    language_name="file_sorter",
    codename="SORTER",
)
XINGCHENG_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "xingcheng",
    group_id=IDENTITY_GROUP_XINGCHENG,
    identity_code="X00001",
    language_name="xingcheng",
    codename="NEBULA",
    physical_root="local-model",
    code_scope="project-source-excluding-governance-rule",
    database_scope=(
        "opaque-central-index-read-and-xingcheng-internal-read-write"
    ),
)
INVESTMENT_MOBILE_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "investment-mobile",
    group_id=IDENTITY_GROUP_INVESTMENT_MOBILE,
    identity_code="I00001",
    language_name="investment_mobile",
    codename="MOBILE",
)
VAULTLY_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "vaultly",
    group_id=IDENTITY_GROUP_VAULTLY,
    identity_code="V00001",
    language_name="vaultly",
    codename="VAULT",
)
del _business_tool_identity

SYSTEM_RESCUE_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=IDENTITY_GROUP_SYSTEM_RESCUE,
    actor="governance/tool/system-rescue",
    bound_tool_id="system-rescue",
    bound_roots=("system-rescue",),
    manifest_binding=ManifestBinding(
        required=True,
        path_template="system-rescue/manifest.json",
        tool_id_field="id",
        maximum_bytes=1_048_576,
        required_capabilities=(
            "system-health-check",
            "central-automatic-repair",
        ),
        requirements=(
            ManifestRequirement(("permissions", "code_scope"), "tool-root-only"),
            ManifestRequirement(("permissions", "database_scope"), "tool-database-only"),
        ),
    ),
    authentication="governance-policy-issued-capability-token",
    identity_code="R00001",
    language_name="system_rescue",
    codename="RESCUE",
)

CAPABILITY_IDENTITIES: Final[tuple[CapabilityIdentity, ...]] = (
    MAIN_SYSTEM_IDENTITY,
    GOVERNANCE_RULE_IDENTITY,
    SHARED_LAYER_IDENTITY,
    AI_ASSISTANT_IDENTITY,
    AI_COLLABORATION_IDENTITY,
    STAR_CHAT_IDENTITY,
    FILE_SORTER_IDENTITY,
    GLOBAL_CLEANER_IDENTITY,
    INVESTMENT_MOBILE_IDENTITY,
    XINGCHENG_IDENTITY,
    VAULTLY_IDENTITY,
    SYSTEM_RESCUE_IDENTITY,
)

ACTIVE_IDENTITY_GROUP: Final[IdentityGroup] = IdentityGroup(
    group_id=ACTIVE_IDENTITY_GROUP_ID,
    identities=CAPABILITY_IDENTITIES,
    management_authority="directory-authority-only",
    registry_mode="hard-coded-immutable",
    legacy_identity_compatibility=False,
    aliases_allowed=False,
    unknown_identity_access="denied",
    authentication="governance-policy-issued-capability-token",
)


def _sealed_registry_reader(group: IdentityGroup):
    def read_registry() -> IdentityGroup:
        return group

    return read_registry


identity_group_snapshot: Final = _sealed_registry_reader(
    ACTIVE_IDENTITY_GROUP
)
del _sealed_registry_reader
