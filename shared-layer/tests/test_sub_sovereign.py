from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent))


def _protocol_members() -> set[str]:
    from governance_rule.execution.tool_runtime.sub_sovereign import SubSovereign

    return set(SubSovereign.__protocol_attrs__)


def test_sub_sovereign_contract_members() -> None:
    members = _protocol_members()
    assert {
        "tool_id",
        "sovereign_id",
        "version",
        "channels",
        "channel_for",
        "health_snapshot",
        "run",
        "role",
    } <= members


def test_sub_sovereign_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        SUB_SOVEREIGN_AUTHORITY,
        SUB_SOVEREIGN_DUTY,
        SUB_SOVEREIGN_ROLE,
        SUB_SOVEREIGN_SCOPE,
        SUB_SOVEREIGN_UNDER,
    )

    assert SUB_SOVEREIGN_ROLE == "sub-sovereign"
    assert "channel-health" in SUB_SOVEREIGN_DUTY
    assert "automatic-cleanup" in SUB_SOVEREIGN_DUTY
    assert "automatic-repair" in SUB_SOVEREIGN_DUTY
    assert "automatic-backup-coordination" in SUB_SOVEREIGN_DUTY
    assert SUB_SOVEREIGN_UNDER == ("system", "maintenance")
    assert SUB_SOVEREIGN_AUTHORITY.startswith(
        "information-management-delivery-channels"
    )
    assert SUB_SOVEREIGN_AUTHORITY.endswith("automatic-cleanup-repair-backup")
    assert SUB_SOVEREIGN_SCOPE == (
        "all-owned-channel-delivery-health-and-local-maintenance-duties"
    )


def test_channel_health_dataclass_shape() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import ChannelHealth

    health = ChannelHealth(channel_id="system")
    snapshot = health.as_dict()
    assert set(snapshot) == {
        "channel_id",
        "last_ok",
        "last_request_at",
        "consecutive_failures",
        "degraded",
    }
    assert snapshot["channel_id"] == "system"
    assert snapshot["last_ok"] is True
    assert snapshot["consecutive_failures"] == 0
    assert snapshot["degraded"] is False


def test_permission_supervisor_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        PERMISSION_SUPERVISOR_AUTHORITY,
        PERMISSION_SUPERVISOR_DUTY,
        PERMISSION_SUPERVISOR_ROLE,
        PERMISSION_SUPERVISOR_SCOPE,
        PERMISSION_SUPERVISOR_UNDER,
    )

    assert PERMISSION_SUPERVISOR_ROLE == "sub-sovereign"
    assert PERMISSION_SUPERVISOR_AUTHORITY == "permission-supervision"
    assert "permission-state-monitor" in PERMISSION_SUPERVISOR_DUTY
    assert "permission-issue-review" in PERMISSION_SUPERVISOR_DUTY
    assert PERMISSION_SUPERVISOR_SCOPE == "permission-supervision-duties"
    assert PERMISSION_SUPERVISOR_UNDER == ("permission",)


def test_permission_granter_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        PERMISSION_GRANTER_AUTHORITY,
        PERMISSION_GRANTER_DUTY,
        PERMISSION_GRANTER_ROLE,
        PERMISSION_GRANTER_SCOPE,
        PERMISSION_GRANTER_UNDER,
    )

    assert PERMISSION_GRANTER_ROLE == "sub-sovereign"
    assert PERMISSION_GRANTER_AUTHORITY == "permission-issue"
    assert "permission-issuance" in PERMISSION_GRANTER_DUTY
    assert "permission-id-assignment" in PERMISSION_GRANTER_DUTY
    assert PERMISSION_GRANTER_SCOPE == "permission-issue-duties"
    assert PERMISSION_GRANTER_UNDER == ("permission",)


def test_permission_revoker_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        PERMISSION_REVOKER_AUTHORITY,
        PERMISSION_REVOKER_DUTY,
        PERMISSION_REVOKER_ROLE,
        PERMISSION_REVOKER_SCOPE,
        PERMISSION_REVOKER_UNDER,
    )

    assert PERMISSION_REVOKER_ROLE == "sub-sovereign"
    assert PERMISSION_REVOKER_AUTHORITY == "permission-termination"
    assert "permission-revocation" in PERMISSION_REVOKER_DUTY
    assert "permission-entitlement-recall" in PERMISSION_REVOKER_DUTY
    assert PERMISSION_REVOKER_SCOPE == "permission-termination-duties"
    assert PERMISSION_REVOKER_UNDER == ("permission",)


def test_maintenance_cleaner_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        MAINTENANCE_CLEANER_AUTHORITY,
        MAINTENANCE_CLEANER_DUTY,
        MAINTENANCE_CLEANER_ROLE,
        MAINTENANCE_CLEANER_SCOPE,
        MAINTENANCE_CLEANER_UNDER,
    )

    assert MAINTENANCE_CLEANER_ROLE == "sub-sovereign"
    assert MAINTENANCE_CLEANER_AUTHORITY == "automatic-cleanup"
    assert "temp-file-cleanup" in MAINTENANCE_CLEANER_DUTY
    assert "cache-cleanup" in MAINTENANCE_CLEANER_DUTY
    assert "empty-directory-cleanup" in MAINTENANCE_CLEANER_DUTY
    assert MAINTENANCE_CLEANER_SCOPE == "automatic-cleanup-duties"
    assert MAINTENANCE_CLEANER_UNDER == ("maintenance",)


def test_maintenance_backer_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        MAINTENANCE_BACKER_AUTHORITY,
        MAINTENANCE_BACKER_DUTY,
        MAINTENANCE_BACKER_ROLE,
        MAINTENANCE_BACKER_SCOPE,
        MAINTENANCE_BACKER_UNDER,
    )

    assert MAINTENANCE_BACKER_ROLE == "sub-sovereign"
    assert MAINTENANCE_BACKER_AUTHORITY == "automatic-backup"
    assert "backup-coordination" in MAINTENANCE_BACKER_DUTY
    assert "backup-integrity-presentation" in MAINTENANCE_BACKER_DUTY
    assert MAINTENANCE_BACKER_SCOPE == "automatic-backup-duties"
    assert MAINTENANCE_BACKER_UNDER == ("maintenance",)


def test_maintenance_repairer_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        MAINTENANCE_REPAIRER_AUTHORITY,
        MAINTENANCE_REPAIRER_DUTY,
        MAINTENANCE_REPAIRER_ROLE,
        MAINTENANCE_REPAIRER_SCOPE,
        MAINTENANCE_REPAIRER_UNDER,
    )

    assert MAINTENANCE_REPAIRER_ROLE == "sub-sovereign"
    assert MAINTENANCE_REPAIRER_AUTHORITY == "automatic-repair"
    assert "damage-isolation" in MAINTENANCE_REPAIRER_DUTY
    assert "repair-execution" in MAINTENANCE_REPAIRER_DUTY
    assert "quarantine-management" in MAINTENANCE_REPAIRER_DUTY
    assert MAINTENANCE_REPAIRER_SCOPE == "automatic-repair-duties"
    assert MAINTENANCE_REPAIRER_UNDER == ("maintenance",)


def test_maintenance_updater_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        MAINTENANCE_UPDATER_AUTHORITY,
        MAINTENANCE_UPDATER_DUTY,
        MAINTENANCE_UPDATER_ROLE,
        MAINTENANCE_UPDATER_SCOPE,
        MAINTENANCE_UPDATER_UNDER,
    )

    assert MAINTENANCE_UPDATER_ROLE == "sub-sovereign"
    assert MAINTENANCE_UPDATER_AUTHORITY == "automatic-update"
    assert "update-management" in MAINTENANCE_UPDATER_DUTY
    assert "update-application" in MAINTENANCE_UPDATER_DUTY
    assert MAINTENANCE_UPDATER_SCOPE == "automatic-update-duties"
    assert MAINTENANCE_UPDATER_UNDER == ("maintenance",)


def test_maintenance_health_monitor_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        MAINTENANCE_HEALTH_MONITOR_AUTHORITY,
        MAINTENANCE_HEALTH_MONITOR_DUTY,
        MAINTENANCE_HEALTH_MONITOR_ROLE,
        MAINTENANCE_HEALTH_MONITOR_SCOPE,
        MAINTENANCE_HEALTH_MONITOR_UNDER,
    )

    assert MAINTENANCE_HEALTH_MONITOR_ROLE == "sub-sovereign"
    assert MAINTENANCE_HEALTH_MONITOR_AUTHORITY == "health-monitoring"
    assert "system-health-monitoring" in MAINTENANCE_HEALTH_MONITOR_DUTY
    assert "health-status-presentation" in MAINTENANCE_HEALTH_MONITOR_DUTY
    assert "health-event-notification" in MAINTENANCE_HEALTH_MONITOR_DUTY
    assert MAINTENANCE_HEALTH_MONITOR_SCOPE == "health-monitoring-duties"
    assert MAINTENANCE_HEALTH_MONITOR_UNDER == ("maintenance",)


def test_system_runtime_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        SYSTEM_RUNTIME_AUTHORITY,
        SYSTEM_RUNTIME_DUTY,
        SYSTEM_RUNTIME_ROLE,
        SYSTEM_RUNTIME_SCOPE,
        SYSTEM_RUNTIME_UNDER,
    )

    assert SYSTEM_RUNTIME_ROLE == "sub-sovereign"
    assert SYSTEM_RUNTIME_AUTHORITY == "runtime"
    assert "process-survival" in SYSTEM_RUNTIME_DUTY
    assert "runtime-integrity" in SYSTEM_RUNTIME_DUTY
    assert SYSTEM_RUNTIME_SCOPE == "runtime-duties"
    assert SYSTEM_RUNTIME_UNDER == ("system",)


def test_system_resource_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        SYSTEM_RESOURCE_AUTHORITY,
        SYSTEM_RESOURCE_DUTY,
        SYSTEM_RESOURCE_ROLE,
        SYSTEM_RESOURCE_SCOPE,
        SYSTEM_RESOURCE_UNDER,
    )

    assert SYSTEM_RESOURCE_ROLE == "sub-sovereign"
    assert SYSTEM_RESOURCE_AUTHORITY == "resource"
    assert "memory-state-monitor" in SYSTEM_RESOURCE_DUTY
    assert "resource-release" in SYSTEM_RESOURCE_DUTY
    assert SYSTEM_RESOURCE_SCOPE == "resource-duties"
    assert SYSTEM_RESOURCE_UNDER == ("system",)


def test_system_data_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        SYSTEM_DATA_AUTHORITY,
        SYSTEM_DATA_DUTY,
        SYSTEM_DATA_ROLE,
        SYSTEM_DATA_SCOPE,
        SYSTEM_DATA_UNDER,
    )

    assert SYSTEM_DATA_ROLE == "sub-sovereign"
    assert SYSTEM_DATA_AUTHORITY == "data"
    assert "consistency-check" in SYSTEM_DATA_DUTY
    assert "integrity-check" in SYSTEM_DATA_DUTY
    assert SYSTEM_DATA_SCOPE == "data-duties"
    assert SYSTEM_DATA_UNDER == ("system",)


def test_system_integration_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        SYSTEM_INTEGRATION_AUTHORITY,
        SYSTEM_INTEGRATION_DUTY,
        SYSTEM_INTEGRATION_ROLE,
        SYSTEM_INTEGRATION_SCOPE,
        SYSTEM_INTEGRATION_UNDER,
    )

    assert SYSTEM_INTEGRATION_ROLE == "sub-sovereign"
    assert SYSTEM_INTEGRATION_AUTHORITY == "integration"
    assert "channel-coordination" in SYSTEM_INTEGRATION_DUTY
    assert "bus-coordination" in SYSTEM_INTEGRATION_DUTY
    assert SYSTEM_INTEGRATION_SCOPE == "integration-duties"
    assert SYSTEM_INTEGRATION_UNDER == ("system",)


def test_system_language_reviewer_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        SYSTEM_LANGUAGE_REVIEWER_AUTHORITY,
        SYSTEM_LANGUAGE_REVIEWER_DUTY,
        SYSTEM_LANGUAGE_REVIEWER_ROLE,
        SYSTEM_LANGUAGE_REVIEWER_SCOPE,
        SYSTEM_LANGUAGE_REVIEWER_UNDER,
    )

    assert SYSTEM_LANGUAGE_REVIEWER_ROLE == "sub-sovereign"
    assert (
        SYSTEM_LANGUAGE_REVIEWER_AUTHORITY == "programming-language-review"
    )
    assert "language-conformance-review" in SYSTEM_LANGUAGE_REVIEWER_DUTY
    assert "language-acceptance-review" in SYSTEM_LANGUAGE_REVIEWER_DUTY
    assert "language-migration-review" in SYSTEM_LANGUAGE_REVIEWER_DUTY
    assert (
        SYSTEM_LANGUAGE_REVIEWER_SCOPE
        == "programming-language-review-duties"
    )
    assert SYSTEM_LANGUAGE_REVIEWER_UNDER == ("system",)


def test_system_third_party_manager_role_constants() -> None:
    from governance_rule.execution.tool_runtime.sub_sovereign import (
        SYSTEM_THIRD_PARTY_MANAGER_AUTHORITY,
        SYSTEM_THIRD_PARTY_MANAGER_DUTY,
        SYSTEM_THIRD_PARTY_MANAGER_ROLE,
        SYSTEM_THIRD_PARTY_MANAGER_SCOPE,
        SYSTEM_THIRD_PARTY_MANAGER_UNDER,
    )

    assert SYSTEM_THIRD_PARTY_MANAGER_ROLE == "sub-sovereign"
    assert (
        SYSTEM_THIRD_PARTY_MANAGER_AUTHORITY
        == "third-party-software-management"
    )
    assert "third-party-introduction-review" in SYSTEM_THIRD_PARTY_MANAGER_DUTY
    assert "third-party-license-review" in SYSTEM_THIRD_PARTY_MANAGER_DUTY
    assert "third-party-security-review" in SYSTEM_THIRD_PARTY_MANAGER_DUTY
    assert (
        SYSTEM_THIRD_PARTY_MANAGER_SCOPE
        == "third-party-software-management-duties"
    )
    assert SYSTEM_THIRD_PARTY_MANAGER_UNDER == ("system",)


def test_governed_runtime_declares_sub_sovereign_members() -> None:
    from governance_rule.execution.tool_runtime.governed_runtime import (
        _SUB_SOVEREIGN_MEMBERS,
        GovernedToolRuntime,
    )

    assert set(_SUB_SOVEREIGN_MEMBERS) == _protocol_members()


def test_governed_runtime_declares_sub_sovereign_class_members() -> None:
    from governance_rule.execution.tool_runtime.governed_runtime import (
        GovernedToolRuntime,
    )

    for member in ("channels", "channel_for", "health_snapshot", "run", "role"):
        assert hasattr(GovernedToolRuntime, member), member


def test_assert_sub_sovereign_rejects_non_conforming() -> None:
    from governance_rule.execution.tool_runtime.governed_runtime import (
        _assert_sub_sovereign,
    )

    class NotASubSovereign:
        tool_id = "x"

    with pytest.raises(AssertionError) as exc:
        _assert_sub_sovereign(NotASubSovereign())
    assert "NOT_SUB_SOVEREIGN" in str(exc.value)


def test_health_snapshot_shape_contract() -> None:
    from governance_rule.execution.tool_runtime.governed_runtime import (
        GovernedToolRuntime,
    )

    class Probe(GovernedToolRuntime):
        def __init__(self) -> None:
            self.tool_id = "probe"
            self.sovereign_id = "system"
            self.version = "1.0.0"
            self.root = ROOT
            self._channels = {"system": object()}  # type: ignore[assignment]
            self._channel_health = {}
            self.health_callback = None
            self.self_repair_enabled = False
            self.local_cleanup_enabled = False
            self._last_self_repair = None
            self._last_local_cleanup = None

    snapshot = Probe().health_snapshot()
    assert snapshot["role"] == "sub-sovereign"
    assert snapshot["sovereign_id"] == "system"
    assert snapshot["subordinate_to"] == ["system", "maintenance"]
    assert snapshot["channels"] == ["system"]
    assert isinstance(snapshot["channel_health"], dict)
    assert isinstance(snapshot["channel_routes"], dict)
    assert "authority" in snapshot and "scope" in snapshot and "duty" in snapshot