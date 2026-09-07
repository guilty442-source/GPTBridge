from __future__ import annotations

from typing import Final

from governance_rule.permission_directory.directory_authority import (
    IMMUTABLE_AUTHORITY_ROOTS,
    AutomaticRepairBoundary,
    CapabilityAuthority,
    CapabilityGrant,
)


CAPABILITY_AUTHORITIES: Final[tuple[CapabilityAuthority, ...]] = (
    CapabilityAuthority(
        "xingcheng-governance-source-read", "xingcheng",
        "direct-authoritative-governance-snapshot-read-only", "none",
        (
            CapabilityGrant(
                "read", "governance-authority-snapshot", "none",
                path_match="within", path_roots=("governance_rule",),
            ),
            CapabilityGrant(
                "read", "permission-directory-snapshot", "none",
                path_match="within",
                path_roots=("governance_rule/permission_directory",),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "governance-authority-read-execute", "tool:governance_rule",
        "own-authority-snapshot-read-and-declared-entry-execute-only", "none",
        (
            CapabilityGrant(
                "read", "governance-authority-snapshot", "none",
                path_match="within", path_roots=("governance_rule",),
            ),
            CapabilityGrant(
                "execute", "tool-runtime:governance_rule", "none",
                path_match="within", path_roots=("governance_rule",),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "permission-directory-read-execute", "tool:governance_rule",
        "managed-directory-snapshot-read-and-declared-module-execute-only", "none",
        (
            CapabilityGrant(
                "read", "permission-directory-snapshot", "none",
                path_match="within",
                path_roots=("governance_rule/permission_directory",),
            ),
            CapabilityGrant(
                "execute", "permission-directory-module", "none",
                path_match="within",
                path_roots=("governance_rule/permission_directory",),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "governance", "main-system", "read-and-enforce-only", "none",
        (
            CapabilityGrant(
                "read", "authority-files", "none", path_match="exact",
                path_roots=IMMUTABLE_AUTHORITY_ROOTS,
            ),
            CapabilityGrant("enforce", "governed-operation", "none"),
            CapabilityGrant("authorize", "capability-request", "none"),
        ), False, False,
    ),
    CapabilityAuthority(
        "authenticated-ipc", "main-system", "authenticated-ipc-only",
        "runtime-state-main-connection-metadata-only",
        (
            CapabilityGrant("connect", "authenticated-ipc", "connection-metadata"),
            CapabilityGrant("disconnect", "authenticated-ipc", "connection-metadata"),
            CapabilityGrant("health-check", "authenticated-ipc", "none"),
        ), False, False,
    ),
    CapabilityAuthority(
        "independent-tool-discovery", "main-system",
        "gptbridge-direct-child-tool-manifest-scan-only", "none",
        (
            CapabilityGrant(
                "discover", "independent-tool-manifests", "none",
                path_match="within", path_roots=(".",),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "independent-tool-start-and-stop", "main-system",
        "process-lifecycle-control-only", "none",
        (
            CapabilityGrant("start", "tool-process:{tool_id}", "none"),
            CapabilityGrant("stop", "tool-process:{tool_id}", "none"),
        ), False, False,
    ),
    CapabilityAuthority(
        "system-channel-request-submit", "governance-policy",
        "governance-authorized-system-channel-submit-cancel-and-consume-only",
        "system-channel-request-database-only",
        tuple(
            CapabilityGrant(
                action, "shared-layer-system-request:{tool_id}",
                "shared-layer-system-request", path_match="exact",
                path_roots=(
                    "postgresql:gptbridge_transport:system",
                ),
            )
            for action in ("request", "cancel-request", "consume-response")
        ), False, False,
    ),
    CapabilityAuthority(
        "system-channel-request-process", "governance-policy",
        "governance-authorized-own-system-request-claim-and-response-only",
        "system-channel-request-database-only",
        tuple(
            CapabilityGrant(
                action, "shared-layer-system-request:{tool_id}",
                "shared-layer-system-request", path_match="exact",
                path_roots=(
                    "postgresql:gptbridge_transport:system",
                ),
            )
            for action in ("claim", "respond")
        ), False, False,
    ),
    CapabilityAuthority(
        "ai-channel-request-submit", "governance-policy",
        "governance-authorized-ai-channel-submit-cancel-and-consume-only",
        "ai-channel-request-database-only",
        tuple(
            CapabilityGrant(
                action, "shared-layer-ai-request:{tool_id}",
                "shared-layer-ai-request", path_match="exact",
                path_roots=(
                    "postgresql:gptbridge_transport:ai",
                ),
            )
            for action in ("request", "cancel-request", "consume-response")
        ), False, False,
    ),
    CapabilityAuthority(
        "ai-channel-request-process", "governance-policy",
        "governance-authorized-own-ai-request-claim-and-response-only",
        "ai-channel-request-database-only",
        tuple(
            CapabilityGrant(
                action, "shared-layer-ai-request:{tool_id}",
                "shared-layer-ai-request", path_match="exact",
                path_roots=(
                    "postgresql:gptbridge_transport:ai",
                ),
            )
            for action in ("claim", "respond")
        ), False, False,
    ),
    CapabilityAuthority(
        "ai-channel-top-level", "governance-policy",
        "star-global-read-and-highest-decision-authority-without-external-execution",
        "global-read-only-except-star-internal-data",
        (
            CapabilityGrant(
                "request-external-collaboration",
                "ai-channel:xingcheng",
                "none",
            ),
            CapabilityGrant(
                "receive-external-response",
                "ai-channel:xingcheng",
                "none",
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "local-model-platform-execution", "governance-policy",
        "all-local-model-loading-selection-inference-and-runtime-lifecycle",
        "local-model-runtime-only",
        (
            CapabilityGrant(
                "execute", "local-model-runtime", "none",
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "star-global-data-read", "tool:xingcheng",
        "global-central-index-visibility-and-governed-owner-resolution-request",
        "opaque-central-index-only",
        (
            CapabilityGrant(
                "read", "central-index", "opaque-resource-index",
            ),
            CapabilityGrant(
                "request-read", "owner-resource-resolve:{tool_id}",
                "opaque-locator-id-only",
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "star-internal-data-read-write", "tool:xingcheng",
        "star-internal-model-data-autonomy-with-read-only-permission-files",
        "xingcheng-private-internal-data-only",
        tuple(
            CapabilityGrant(
                action, "star-internal-data", "xingcheng-internal-data",
                path_match="within", path_roots=("local-model/xingcheng",),
                excluded_path_roots=("local-model/xingcheng/permissions",),
            )
            for action in (
                "read",
                "append",
                "delete",
                "rollback",
                "update",
                "write",
            )
        ), False, False,
    ),
    CapabilityAuthority(
        "star-decision", "governance-policy",
        "highest-non-governance-decision-authority-no-execution",
        "structured-decision-only",
        (
            CapabilityGrant("approve", "governed-decision", "none"),
            CapabilityGrant("deny", "governed-decision", "none"),
            CapabilityGrant("stop", "governed-decision", "none"),
            CapabilityGrant("request-reassessment", "governed-decision", "none"),
        ), False, False,
    ),
    CapabilityAuthority(
        "star-investment-manager-database-read", "tool:xingcheng",
        "ai-assistant-investment-database-read-only", "none",
        (
            CapabilityGrant(
                "read", "tool-business-storage:ai-assistant",
                "ai-assistant-investment-database",
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "hot-update", "main-system", "versioned-tool-code-update-only",
        "tool-code-only-excluding-authority-files-and-business-data",
        (
            CapabilityGrant(
                "update", "tool-code:{tool_id}", "tool-code", "increment",
                "within", ("{tool_id}",),
                (
                    "{tool_id}/runtime",
                    "{tool_id}/data",
                ),
            ),
            CapabilityGrant(
                "rollback", "tool-code:{tool_id}", "tool-code", "increment",
                "within", ("{tool_id}",),
                (
                    "{tool_id}/runtime",
                    "{tool_id}/data",
                ),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "frontend-backend-connection-stability", "main-system",
        "authenticated-connection-lifecycle-only", "runtime-state-main-only",
        (
            CapabilityGrant("diagnose", "frontend-backend-connection", "none"),
            CapabilityGrant(
                "stabilize", "frontend-backend-connection", "main-runtime-state",
                path_match="within", path_roots=("main-system/runtime/state",),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "independent-tool-business-logic", "tool:{tool_id}",
        "own-tool-source-and-runtime-only",
        "none-without-separate-storage-capability",
        (
            CapabilityGrant(
                "execute", "tool-runtime:{tool_id}", "none",
                path_match="within", path_roots=("{tool_id}",),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "independent-tool-user-settings", "tool:{tool_id}",
        "own-user-settings-operations-only", "own-runtime-settings-database-only",
        tuple(
            CapabilityGrant(
                action, "tool-settings:{tool_id}", "tool-settings:{tool_id}",
                path_match="within",
                path_roots=("{tool_id}/runtime/settings",),
            )
            for action in ("read", "write")
        ), False, False,
    ),
    CapabilityAuthority(
        "independent-tool-business-storage", "tool:{tool_id}",
        "own-business-storage-operations-only", "own-business-database-only",
        tuple(
            CapabilityGrant(
                action, "tool-business-storage:{tool_id}",
                "tool-business:{tool_id}", path_match="within",
                path_roots=("{tool_id}/data/business",),
            )
            for action in ("read", "write")
        ), False, False,
    ),
    CapabilityAuthority(
        "global-cleanup", "tool:global-cleaner",
        "gptbridge-global-garbage-and-excess-log-cleanup-only",
        "delete-only-no-general-storage-write",
        (
            CapabilityGrant(
                "read", "global-cleanup-candidates", "global-read-only",
                path_match="within", path_roots=(".",),
            ),
            CapabilityGrant(
                "delete", "validated-global-garbage", "none",
                path_match="within", path_roots=(".",),
                excluded_path_roots=IMMUTABLE_AUTHORITY_ROOTS,
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "managed-backup", "tool:global-cleaner",
        "per-owner-backup-create-verify-retain-and-governed-extract-only",
        "global-cleaner-backup-and-shared-extract-staging-only",
        (
            CapabilityGrant(
                "read-source", "backup-owner-source", "global-read-only",
                path_match="within", path_roots=(".",),
                excluded_path_roots=IMMUTABLE_AUTHORITY_ROOTS,
            ),
            CapabilityGrant(
                "create-backup", "managed-backup-root", "owner-scoped-backup",
                path_match="within",
                path_roots=("global-cleaner/data/business/backups",),
            ),
            CapabilityGrant(
                "delete-excess", "managed-backup-root", "owner-scoped-backup",
                path_match="within",
                path_roots=("global-cleaner/data/business/backups",),
            ),
            CapabilityGrant(
                "extract-backup", "backup-extract-staging", "backup-extract",
                path_match="within",
                path_roots=(
                    "global-cleaner/runtime/temp/shared-layer/backup-extract",
                ),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "system-health-check", "tool:global-cleaner",
        "gptbridge-global-system-health-read-only", "none",
        (
            CapabilityGrant(
                "check", "global-system-health", "global-read-only",
                path_match="within", path_roots=(".",),
            ),
        ), False, False,
    ),
    CapabilityAuthority(
        "central-automatic-repair", "tool:system-rescue",
        "gptbridge-central-automatic-repair-only", "none",
        (
            CapabilityGrant(
                "repair", "automatic-repair-database:{tool_id}", "tool-stability:{tool_id}",
                path_match="within", path_roots=("main-system/data/automatic-repair",),
            ),
            CapabilityGrant(
                "diagnose", "global-system-health", "global-read-only",
                path_match="within", path_roots=(".",),
            ),
        ), False, False,
    ),
)

AUTOMATIC_REPAIR_BOUNDARIES: Final[tuple[AutomaticRepairBoundary, ...]] = (
    AutomaticRepairBoundary(
        owner="main-system",
        allowed_scope="main-shared-and-target-tool-stability-with-per-target-database-only",
        authorization_source="governance-policy-issued-capability-token-only",
        authorization_failure_code="PERMISSION_DENIED",
        prohibited_scope=(
            "authority-files",
            "tool-source-code",
            "cross-target-database-access",
            "direct-managed-backup-access",
            "business-features",
            "business-rules",
        ),
    ),
)


def _sealed_boundary_reader(
    capabilities: tuple[CapabilityAuthority, ...],
    repairs: tuple[AutomaticRepairBoundary, ...],
):
    def read_boundaries() -> tuple[
        tuple[CapabilityAuthority, ...],
        tuple[AutomaticRepairBoundary, ...],
    ]:
        return capabilities, repairs

    return read_boundaries


capability_boundary_snapshot: Final = _sealed_boundary_reader(
    CAPABILITY_AUTHORITIES,
    AUTOMATIC_REPAIR_BOUNDARIES,
)
del _sealed_boundary_reader
