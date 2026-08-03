from __future__ import annotations

from typing import Final

from governance_rule.permission_directory.directory_authority import (
    ACTIVE_IDENTITY_GROUP_ID,
    IdentityPermissionBinding,
)


IDENTITY_PERMISSION_BINDINGS: Final[
    tuple[IdentityPermissionBinding, ...]
] = (
    IdentityPermissionBinding(
        group_id=ACTIVE_IDENTITY_GROUP_ID,
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
        group_id=ACTIVE_IDENTITY_GROUP_ID,
        actor="governance/tool/governance_rule",
        capabilities=(
            "governance-authority-read-execute",
            "permission-directory-read-execute",
            "system-channel-request-submit",
            "system-channel-request-process",
        ),
    ),
    IdentityPermissionBinding(
        group_id=ACTIVE_IDENTITY_GROUP_ID,
        actor="governance/tool/shared-layer",
        capabilities=(
            "authenticated-ipc",
            "system-channel-request-submit",
            "system-channel-request-process",
        ),
    ),
    IdentityPermissionBinding(
        group_id=ACTIVE_IDENTITY_GROUP_ID,
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
        group_id=ACTIVE_IDENTITY_GROUP_ID,
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
        group_id=ACTIVE_IDENTITY_GROUP_ID,
        actor="governance/tool/star-chat",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
            "ai-channel-request-submit",
        ),
    ),
    IdentityPermissionBinding(
        group_id=ACTIVE_IDENTITY_GROUP_ID,
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
        group_id=ACTIVE_IDENTITY_GROUP_ID,
        actor="governance/tool/global-cleaner",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
            "global-cleanup",
            "managed-backup",
            "system-health-check",
        ),
    ),
    IdentityPermissionBinding(
        group_id=ACTIVE_IDENTITY_GROUP_ID,
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
    IdentityPermissionBinding(
        group_id=ACTIVE_IDENTITY_GROUP_ID,
        actor="governance/tool/local-ai",
        capabilities=(
            "ai-channel-top-level",
            "star-global-data-read",
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
    IdentityPermissionBinding(
        group_id=ACTIVE_IDENTITY_GROUP_ID,
        actor="governance/tool/vaultly",
        capabilities=(
            "independent-tool-business-logic",
            "independent-tool-user-settings",
            "independent-tool-business-storage",
            "system-channel-request-submit",
            "system-channel-request-process",
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
