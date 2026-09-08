from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
SHARED_SRC = Path(__file__).resolve().parents[2] / "shared-layer" / "src"
sys.path.insert(0, str(SRC_CORE))
sys.path.insert(0, str(SHARED_SRC))

from core_system.daily_global_cleaner_service import DailyGlobalCleanerService
from core_system.main_system_self_maintenance import MainSystemSelfMaintenance


@pytest.mark.asyncio
async def test_daily_cleaner_uses_app_permission_sovereign(tmp_path: Path) -> None:
    class Permission:
        def tool_execution_response(
            self, tool_id: str, request_id: str
        ) -> dict[str, object]:
            assert tool_id == "global-cleaner"
            assert request_id.startswith("daily-global-cleaner-")
            return {
                "status": "completed",
                "response": {"ok": True, "cleaned_bytes": 0},
            }

    class Toolbox:
        async def start_tool(self, _payload: dict[str, object]) -> dict[str, object]:
            return {"ok": True, "message": "already running"}

        async def request_tool_execution(
            self, _payload: dict[str, object]
        ) -> dict[str, object]:
            return {"ok": True}

    permission = Permission()
    app = SimpleNamespace(
        project_root=tmp_path,
        permission_sovereign=permission,
        system_sovereign_service=SimpleNamespace(),
        toolbox_service=Toolbox(),
    )
    service = DailyGlobalCleanerService(app)

    async def cleanup_sweep() -> dict[str, object]:
        return {"ok": True, "module_count": 1}

    service._run_module_self_cleanup_sweep = cleanup_sweep
    result = await service.run_if_due(force=True)

    assert service._permission_master_entry() is permission
    assert result["ok"] is True
    assert result["stage"] == "completed"
    assert service.status()["owner"] == "maintenance-sovereign"
    assert service.module_cleanup_status()["ok"] is True


@pytest.mark.asyncio
async def test_main_system_cleanup_has_one_scheduler(tmp_path: Path) -> None:
    service = MainSystemSelfMaintenance(tmp_path)

    async def successful_duty() -> dict[str, object]:
        return {"ok": True}

    service._duty_source_self_repair = successful_duty
    service._duty_integrity_verify = successful_duty
    report = await service.run_once()

    assert not hasattr(service, "_duty_local_cleanup")
    assert report["duties"]["local_cleanup"] == {
        "ok": True,
        "skipped": True,
        "reason": "DAILY_GLOBAL_CLEANER_OWNS_SCHEDULE",
        "delegated_to": "daily-global-cleaner",
    }
