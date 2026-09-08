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
