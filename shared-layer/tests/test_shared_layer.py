"""shared-layer consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "global-cleaner" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

########################################################################
# source: shared-layer/tests/test_architecture_contract.py
########################################################################
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))


def test_local_sql_engine_declared_and_no_physical_content_in_index() -> None:
    from governance_rule.permission_directory.directory_authority import (
        directory_authority_snapshot,
    )
    from governance_rule.governance_policy import governance_policy_snapshot

    authority = directory_authority_snapshot().shared_layer_access_policy
    policy = governance_policy_snapshot().shared_layer
    assert authority.database_path.startswith("postgresql:")
    assert authority.ai_database_path.startswith("postgresql:")
    assert authority.database_path != authority.ai_database_path
    assert authority.central_index_engine == "postgresql"
    assert policy.database_path == authority.database_path
    assert policy.ai_database_path == authority.ai_database_path


def test_xingcheng_has_select_only_grants() -> None:
    sql = (ROOT / "sql" / "central_index.sql").read_text(encoding="utf-8")
    star_lines = [line.strip() for line in sql.splitlines() if "gptbridge_xingcheng_reader" in line]
    assert star_lines
    assert not any(line.startswith("GRANT INSERT") or line.startswith("GRANT UPDATE") or line.startswith("GRANT DELETE") for line in star_lines)
    migration = (ROOT / "migrations" / "001_registry_locations.sql").read_text(encoding="utf-8")
    assert "physical_location" not in migration.split("CREATE OR REPLACE VIEW registry.resource_locations", 1)[1].split("FROM registry.locations", 1)[0]


def test_python_gateway_is_default_deny_and_xingcheng_read_only() -> None:
    from governance_rule.permission_directory.execution.access_gateway import (
        gateway as module,
    )

    denied = module.AccessGateway(lambda *_: False)
    own = module.Principal("tool-a", "file-sorter")
    assert denied.decide(own, "read", "file-sorter").allowed is False
    assert denied.decide(own, "read", "vaultly").reason == "CROSS_MODULE_DEFAULT_DENY"
    star = module.Principal("xingcheng", "xingcheng", is_xingcheng=True)
    allowed = module.AccessGateway(lambda *_: True)
    assert allowed.decide(star, "read", "vaultly").allowed is True
    assert allowed.decide(star, "update", "vaultly").reason == "XINGCHENG_CROSS_MODULE_READ_ONLY"
    assert allowed.decide(star, "update", "xingcheng").allowed is True
    assert allowed.decide(star, "manage", "xingcheng").allowed is True
    assert allowed.decide(
        star, "update", "xingcheng", resource_class="permission-file"
    ).reason == "PROTECTED_AUTHORITY_READ_ONLY"
    assert allowed.decide(
        star, "read", "xingcheng", resource_class="permission-file"
    ).allowed is True
    assert allowed.decide(star, "execute", "governance_rule").allowed is False


def test_local_rag_runtime_is_fixed_location(tmp_path: Path) -> None:
    sys.path.insert(
        0,
        str(
            ROOT.parent
            / "Standalone tools"
            / "local-model"
            / "src"
            / "backend"
            / "services"
        ),
    )
    from xingcheng.infrastructure.rag_bridge import local_runtime as module

    runtime = module.runtime_for(tmp_path)
    assert runtime.index_root == (tmp_path / "shared-layer" / "runtime" / "semantic-index").resolve()
    try:
        module.LocalRagRuntime(tmp_path / "private" / "runtime" / "qdrant")
    except ValueError as exc:
        assert str(exc) == "LOCAL_SEMANTIC_INDEX_LOCATION_INVALID"
    else:
        raise AssertionError("out-of-contract index root accepted")


def test_local_hits_require_authorization_and_no_content_payload() -> None:
    sys.path.insert(
        0,
        str(
            ROOT.parent
            / "Standalone tools"
            / "local-model"
            / "src"
            / "backend"
            / "services"
        ),
    )
    from xingcheng.infrastructure.rag_bridge import bridge as module

    hits = (
        module.QdrantHit("R1", "C1", "vaultly", 0.9),
        module.QdrantHit("R2", "C2", "file-sorter", 0.8),
    )
    bridge = module.RagAuthorizationBridge(lambda _actor, resource: resource == "R2")
    assert tuple(hit.resource_id for hit in bridge.filter_authorized("xingcheng", hits)) == ("R2",)

    class Store:
        def replace_document(self, *_args, **_kwargs):
            raise AssertionError("invalid payload reached the index")

    coordinator = module.RagIndexCoordinator(Store(), lambda *_: None, lambda *_: None)
    try:
        coordinator.upsert("R1", "vaultly", [{"id": "P1", "payload": {"chunk_id": "C1", "content": "secret"}}])
    except ValueError as exc:
        assert str(exc) == "RAG_PAYLOAD_MUST_NOT_CONTAIN_PHYSICAL_CONTENT_OR_PATH"
    else:
        raise AssertionError("physical content was accepted into index payload")


def test_no_installer_or_docker_dependency_in_python_core() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "shared_layer").rglob("*.py"))
    forbidden = ("pip install", "winget install", "choco install", "docker compose", "docker run")
    assert not any(command in sources.casefold() for command in forbidden)


def test_xingcheng_self_database_write_is_executor_only() -> None:
    manifest = json.loads((ROOT.parent / "Standalone tools" / "local-model" / "manifest.json").read_text(encoding="utf-8"))
    star = manifest["capabilities"]["xingcheng"]["star_native_model_permissions"]
    assert star["database_write"] is True
    assert star["database_write_scope"] == "xingcheng-model-internal-unrestricted-excluding-permission-data"
    assert star["investment_database_write"] is True
    assert manifest["permissions"]["database_scope"] == "opaque-central-index-read-and-xingcheng-internal-read-write"



########################################################################
# source: shared-layer/tests/test_sub_sovereign.py
########################################################################
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _protocol_members() -> set[str]:
    from governance_rule.execution.tool_runtime.sub_sovereign import SubSovereign

    attrs = set(getattr(SubSovereign, "__protocol_attrs__", set()))
    if not attrs:
        attrs = set(getattr(SubSovereign, "__annotations__", {})) | {
            name for name in dir(SubSovereign) if not name.startswith("_")
        }
    return attrs


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

    assert SUB_SOVEREIGN_ROLE.endswith("-sub-sovereign")
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

    assert PERMISSION_SUPERVISOR_ROLE.endswith("-sub-sovereign")
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

    assert PERMISSION_GRANTER_ROLE.endswith("-sub-sovereign")
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

    assert PERMISSION_REVOKER_ROLE.endswith("-sub-sovereign")
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

    assert MAINTENANCE_CLEANER_ROLE.endswith("-sub-sovereign")
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

    assert MAINTENANCE_BACKER_ROLE.endswith("-sub-sovereign")
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

    assert MAINTENANCE_REPAIRER_ROLE.endswith("-sub-sovereign")
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

    assert MAINTENANCE_UPDATER_ROLE.endswith("-sub-sovereign")
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

    assert MAINTENANCE_HEALTH_MONITOR_ROLE.endswith("-sub-sovereign")
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

    assert SYSTEM_RUNTIME_ROLE.endswith("-sub-sovereign")
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

    assert SYSTEM_RESOURCE_ROLE.endswith("-sub-sovereign")
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

    assert SYSTEM_DATA_ROLE.endswith("-sub-sovereign")
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

    assert SYSTEM_INTEGRATION_ROLE.endswith("-sub-sovereign")
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

    assert SYSTEM_LANGUAGE_REVIEWER_ROLE.endswith("-sub-sovereign")
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

    assert SYSTEM_THIRD_PARTY_MANAGER_ROLE.endswith("-sub-sovereign")
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
    assert snapshot["role"].endswith("-sub-sovereign")
    assert snapshot["sovereign_id"] == "system"
    assert snapshot["subordinate_to"] == ["system", "maintenance"]
    assert snapshot["channels"] == ["system"]
    assert isinstance(snapshot["channel_health"], dict)
    assert isinstance(snapshot["channel_routes"], dict)
    assert "authority" in snapshot and "scope" in snapshot and "duty" in snapshot
