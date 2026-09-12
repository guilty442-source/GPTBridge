from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC_CORE = ROOT / "src-core"
SHARED_SRC = ROOT / "shared-layer" / "src"
GOVERNANCE_RULE = ROOT / "governance_rule"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC_CORE))
sys.path.insert(0, str(SHARED_SRC))
sys.path.insert(0, str(GOVERNANCE_RULE))

from core_system import (
    MaintenanceSovereign,
    DataSubSovereign,
    LanguageReviewSubSovereign,
    RuntimeSubSovereign,
    ThirdPartySubSovereign,
)


def test_update_management_has_one_owner() -> None:
    # HealthMaintenanceTestSubSovereign has health monitoring methods only
    assert hasattr(MaintenanceSovereign, "_update_status")
    assert hasattr(MaintenanceSovereign, "_automatic_repair_status")
    assert hasattr(MaintenanceSovereign, "_fault_determination_status")
    assert hasattr(MaintenanceSovereign, "_backup_status")
    # Execution methods are on MaintenanceUpdateMixin (used by runtime), not the sovereign
    assert not hasattr(RuntimeSubSovereign, "hot_update_status")
    assert not hasattr(ThirdPartySubSovereign, "execute_update")
    assert not hasattr(ThirdPartySubSovereign, "execute_auto_updates")


@pytest.mark.asyncio
async def test_maintenance_delegates_approved_update_execution() -> None:
    calls: list[tuple[str, str | None]] = []

    class Executor:
        async def apply_approved_update(
            self, tool_id: str, *, approval_token: str | None = None
        ) -> str:
            calls.append((tool_id, approval_token))
            return "applied"

    # New architecture: decision_sovereign.third_party_sovereign -> dependency-sync-sub-sovereign
    app = SimpleNamespace(
        decision_sovereign=SimpleNamespace(third_party_sovereign=Executor())
    )
    # Use MaintenanceUpdateMixin directly for testing execution delegation
    from core_system.maintenance_update import MaintenanceUpdateMixin

    class TestSovereign(MaintenanceUpdateMixin):
        def __init__(self, app):
            self.app = app

    sovereign = TestSovereign(app)

    result = await sovereign.execute_third_party_update(
        "uv", approval_token="approved"
    )

    assert result == "applied"
    assert calls == [("uv", "approved")]


def test_maintenance_is_the_only_governance_health_checker(tmp_path: Path) -> None:
    calls = 0

    class Governance:
        def runtime_integrity_ready(self) -> bool:
            nonlocal calls
            calls += 1
            return True

    app = SimpleNamespace(
        project_root=tmp_path,
        governance=Governance(),
        governance_integrity_ready=None,
    )
    maintenance = MaintenanceSovereign(app)
    maintenance._health_checker = lambda _root: {"ok": True}
    maintenance._health_monitoring()

    RuntimeSubSovereign(app).live_status()
    language = LanguageReviewSubSovereign(app)
    language._python_audit = app.governance
    language._python_audit_status()
    data = DataSubSovereign(app)
    data._data_directory_report()

    assert calls == 1
    assert app.governance_integrity_ready is True


def test_maintenance_is_the_only_cleaner_and_repair_executor_owner() -> None:
    app = SimpleNamespace()
    maintenance = MaintenanceSovereign(app)
    maintenance._daily_cleaner = object()
    maintenance._repair_service = object()
    app.maintenance_sovereign = maintenance
    data = DataSubSovereign(app)

    ownership = maintenance.executor_ownership_status()
    consistency = data._consistency_integrity_status()
    directory = data._data_directory_status()

    # New sovereign role is health-maintenance-test-sub-sovereign
    assert ownership == {
        "owner": "health-maintenance-test-sub-sovereign",
        "daily_global_cleaner": True,
        "central_repair": True,
    }
    assert consistency["repair_delegated"] is True
    assert consistency["executor_owner"] == "health-maintenance-test-sub-sovereign"
    assert directory["cleanup_delegated"] is True
    assert directory["executor_owner"] == "health-maintenance-test-sub-sovereign"
    assert "_daily_cleaner" not in vars(data)
    assert "_repair_service" not in vars(data)