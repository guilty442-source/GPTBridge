from __future__ import annotations

from typing import Final

from governance_rule.permission_directory.directory_authority import (
    ACTIVE_IDENTITY_GROUP_ID,
    CapabilityIdentity,
    IdentityGroup,
    ManifestBinding,
    ManifestRequirement,
)


MAIN_SYSTEM_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=ACTIVE_IDENTITY_GROUP_ID,
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
)

GOVERNANCE_RULE_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=ACTIVE_IDENTITY_GROUP_ID,
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
)

SHARED_LAYER_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=ACTIVE_IDENTITY_GROUP_ID,
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
)

GLOBAL_CLEANER_IDENTITY: Final[CapabilityIdentity] = CapabilityIdentity(
    group_id=ACTIVE_IDENTITY_GROUP_ID,
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
)


def _business_tool_identity(
    tool_id: str,
    *,
    physical_root: str | None = None,
    code_scope: str = "tool-root-only",
    database_scope: str = "tool-database-only",
) -> CapabilityIdentity:
    owner_root = str(physical_root or tool_id).strip()
    return CapabilityIdentity(
        group_id=ACTIVE_IDENTITY_GROUP_ID,
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
    )


AI_ASSISTANT_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "ai-assistant"
)
AI_COLLABORATION_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "ai-collaboration"
)
STAR_CHAT_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "star-chat",
    physical_root="local-model/model-dialogue",
    database_scope=(
        "all-project-databases-via-xingcheng-excluding-governance-rule"
    ),
)
FILE_SORTER_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "file-sorter"
)
XINGCHENG_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "xingcheng",
    physical_root="local-model",
    code_scope="project-source-excluding-governance-rule",
    database_scope=(
        "opaque-central-index-read-and-xingcheng-internal-read-write"
    ),
)
INVESTMENT_MOBILE_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "investment-mobile"
)
VAULTLY_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "vaultly"
)
SYSTEM_RESCUE_IDENTITY: Final[CapabilityIdentity] = _business_tool_identity(
    "system-rescue"
)
del _business_tool_identity

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
    authentication="governance-policy-issued-capability-token-only",
)


def _sealed_registry_reader(group: IdentityGroup):
    def read_registry() -> IdentityGroup:
        return group

    return read_registry


identity_group_snapshot: Final = _sealed_registry_reader(
    ACTIVE_IDENTITY_GROUP
)
del _sealed_registry_reader
