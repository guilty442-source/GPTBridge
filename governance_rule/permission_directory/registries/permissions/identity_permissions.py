from __future__ import annotations

from typing import Final

from governance_rule.permission_directory.directory_authority import (
    IDENTITY_GROUP_AI_ASSISTANT,
    IDENTITY_GROUP_AI_COLLABORATION,
    IDENTITY_GROUP_FILE_SORTER,
    IDENTITY_GROUP_GLOBAL_CLEANER,
    IDENTITY_GROUP_GOVERNANCE_RULE,
    IDENTITY_GROUP_INVESTMENT_MOBILE,
    IDENTITY_GROUP_LOCAL_MODEL,
    IDENTITY_GROUP_LOCAL_MODEL_DIALOGUE,
    IDENTITY_GROUP_LOCAL_MODEL_STAR_CHAT,
    IDENTITY_GROUP_MAIN_SYSTEM,
    IDENTITY_GROUP_SHARED_LAYER,
    IDENTITY_GROUP_SYSTEM_RESCUE,
    IDENTITY_GROUP_VAULTLY,
    IDENTITY_GROUP_XINGCHENG,
    IDENTITY_GROUP_XINGCHENG_ASSISTANT,
    IdentityPermissionBinding,
)


IDENTITY_PERMISSION_BINDINGS: Final[
    tuple[IdentityPermissionBinding, ...]
] = (
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_MAIN_SYSTEM,
        actor="governance/main-system",
        capabilities=(
            "governance",
            "authenticated-ipc",
            "independent-tool-discovery",
            "independent-tool-start-and-stop",
            "system-channel-request-submit",
            "hot-update",
            "frontend-backend-connection-stability",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_GOVERNANCE_RULE,
        actor="governance/tool/governance_rule",
        capabilities=(
            "codex-read-execute",
            "permission-directory-read-execute",
            "system-channel-request-submit",
            "system-channel-request-process",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_SHARED_LAYER,
        actor="governance/tool/shared-layer",
        capabilities=(
            "authenticated-ipc",
            "system-channel-request-submit",
            "system-channel-request-process",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_AI_ASSISTANT,
        actor="governance/tool/ai-assistant",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
            "ai-channel-request-submit",
            "ai-channel-request-process",
            "local-model-platform-execution",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_AI_COLLABORATION,
        actor="governance/tool/ai-collaboration",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
            "ai-channel-request-submit",
            "ai-channel-request-process",
        ),
    ),

    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_FILE_SORTER,
        actor="governance/tool/file-sorter",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
        ),
    ),
    IdentityPermissionBinding(
        # Retired identity (A533/A534): residual executable capabilities
        # stripped 2026-09-22 (E14 directory deletion); generic channel and
        # own-scope capabilities remain as lineage evidence only.
        group_id=IDENTITY_GROUP_GLOBAL_CLEANER,
        actor="governance/tool/global-cleaner",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_INVESTMENT_MOBILE,
        actor="governance/tool/investment-mobile",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
            "ai-channel-request-submit",
        ),
    ),
    # 星澄 — native model / own-domain sovereign (X00001): model inference
    # and owned-domain autonomy only.  Institution review powers live on the
    # 星澄助理 identity group (non-transitive groups, A156).
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_XINGCHENG,
        actor="governance/tool/xingcheng",
        capabilities=(
            "ai-channel-top-level",
            "star-internal-data-read-write",
            "star-decision",
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
            "ai-channel-request-submit",
            "ai-channel-request-process",
        ),
    ),
    # 星澄助理 — auxiliary system / independent privileged institution
    # (X00002): codex read, global read-only review evidence and user
    # notification powers (A144/A145/A156); no decision or execution power.
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_XINGCHENG_ASSISTANT,
        actor="governance/tool/xingcheng-assistant",
        capabilities=(
            "system-channel-request-submit",
            "system-channel-request-process",
            "xingcheng-governance-source-read",
            "xingcheng-fault-analysis-read",
            "star-global-data-read",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_VAULTLY,
        actor="governance/tool/vaultly",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_SYSTEM_RESCUE,
        actor="governance/tool/system-rescue",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
            "system-health-check",
            "central-automatic-repair",
            "managed-backup",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_LOCAL_MODEL,
        actor="governance/tool/local-model",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
        ),
    ),
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_LOCAL_MODEL_DIALOGUE,
        actor="governance/tool/model-dialogue",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
            "ai-channel-request-submit",
        ),
    ),
    # star-chat is a model-dialogue companion tool (manifest:
    # companion_tool=true, host_tool_id=model-dialogue); it is a route
    # actor on the AI channel so it needs channel-submit/process plus the
    # AI-channel submit grant.  Business surfaces stay on the host.
    IdentityPermissionBinding(
        group_id=IDENTITY_GROUP_LOCAL_MODEL_STAR_CHAT,
        actor="governance/tool/star-chat",
        capabilities=(
            "system-channel-request-submit",
            "system-channel-request-process",
            "ai-channel-request-submit",
        ),
    ),
)


def _sealed_permission_reader(
    bindings: tuple[IdentityPermissionBinding, ...],
):
    def read_permissions() -> tuple[IdentityPermissionBinding, ...]:
        return bindings

    return read_permissions


identity_permission_snapshot: Final = _sealed_permission_reader(
    IDENTITY_PERMISSION_BINDINGS
)
del _sealed_permission_reader
