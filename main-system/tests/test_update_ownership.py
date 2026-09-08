from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
SHARED_SRC = Path(__file__).resolve().parents[2] / "shared-layer" / "src"
sys.path.insert(0, str(SRC_CORE))
sys.path.insert(0, str(SHARED_SRC))

from core_system.maintenance_sovereign import MaintenanceSovereign
from core_system.data_sub_sovereign import DataSubSovereign
from core_system.language_review_sub_sovereign import LanguageReviewSubSovereign
from core_system.runtime_sub_sovereign import RuntimeSubSovereign
from core_system.third_party_sub_sovereign import ThirdPartySubSovereign


def test_update_management_has_one_owner() -> None:
    assert hasattr(MaintenanceSovereign, "execute_third_party_update")
    assert hasattr(MaintenanceSovereign, "execute_auto_third_party_updates")
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

    app = SimpleNamespace(
        system_sovereign_service=SimpleNamespace(third_party_sovereign=Executor())
    )
    sovereign = MaintenanceSovereign(app)

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

    assert ownership == {
        "owner": "maintenance-sovereign",
        "daily_global_cleaner": True,
        "central_repair": True,
    }
    assert consistency["repair_delegated"] is True
    assert consistency["executor_owner"] == "maintenance-sovereign"
    assert directory["cleanup_delegated"] is True
    assert directory["executor_owner"] == "maintenance-sovereign"
    assert "_daily_cleaner" not in vars(data)
    assert "_repair_service" not in vars(data)
