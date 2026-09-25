"""Update-execution ownership under the five-core model (A604).

The retired sub-sovereign layer (``MaintenanceSovereign`` and the
``*-sub-sovereign`` identities) is gone.  Third-party update execution is
delegated to the governed ``ThirdPartyManager`` module owned by
automation-core (``automation_sovereign.third_party_manager``).
"""

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


@pytest.mark.asyncio
async def test_maintenance_delegates_approved_update_execution() -> None:
    calls: list[tuple[str, str | None]] = []

    class Executor:
        async def execute_update(
            self, tool_id: str, *, approval_token: str | None = None
        ) -> str:
            calls.append((tool_id, approval_token))
            return "applied"

    # A604: third-party-dependency domain normalized to automation-core;
    # the governed module is automation_sovereign.third_party_manager.
    app = SimpleNamespace(
        automation_sovereign=SimpleNamespace(third_party_manager=Executor())
    )
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


@pytest.mark.asyncio
async def test_maintenance_delegates_approved_auto_update_execution() -> None:
    calls: list[tuple[str, bool]] = []

    class Executor:
        async def execute_auto_updates(
            self, *, approval_token: str, only_available: bool = True
        ) -> str:
            calls.append((approval_token, only_available))
            return "applied"

    app = SimpleNamespace(
        automation_sovereign=SimpleNamespace(third_party_manager=Executor())
    )
    from core_system.maintenance_update import MaintenanceUpdateMixin

    class TestSovereign(MaintenanceUpdateMixin):
        def __init__(self, app):
            self.app = app

    sovereign = TestSovereign(app)

    result = await sovereign.execute_auto_third_party_updates(
        approval_token="approved", only_available=False
    )

    assert result == "applied"
    assert calls == [("approved", False)]


@pytest.mark.asyncio
async def test_third_party_update_fails_closed_without_module() -> None:
    """No automation core / no module -> fail closed, never silent."""
    from core_system.maintenance_update import MaintenanceUpdateMixin

    class TestSovereign(MaintenanceUpdateMixin):
        def __init__(self, app):
            self.app = app

    sovereign = TestSovereign(SimpleNamespace(automation_sovereign=None))
    with pytest.raises(RuntimeError, match="not available"):
        await sovereign.execute_third_party_update("uv")
