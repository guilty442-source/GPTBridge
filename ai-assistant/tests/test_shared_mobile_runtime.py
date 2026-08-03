from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = TOOL_ROOT.parent
SHARED_SERVICES = PROJECT_ROOT / "shared-layer" / "src"
MOBILE_SERVICES = (
    TOOL_ROOT
    / "investment-mobile"
    / "src"
    / "backend"
    / "services"
)
for services_path in (SHARED_SERVICES, MOBILE_SERVICES):
    if str(services_path) not in sys.path:
        sys.path.insert(0, str(services_path))

from ai_nexus.application.mobile_bridge import InvestmentMobileBridgeMixin  # noqa: E402
from investment_mobile.application.service import InvestmentMobileService  # noqa: E402


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def set_setting(self, key: str, value: Any) -> None:
        self.values[key] = value

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class EmptyInvestmentRepository:
    @staticmethod
    def load_state() -> dict[str, Any]:
        return {"holdings": []}


class MobileBridgeHarness(InvestmentMobileBridgeMixin):
    def __init__(self) -> None:
        self.analytics_store = MemorySettings()
        self.repository = EmptyInvestmentRepository()
        self._mobile_sync_start_error = ""

    @staticmethod
    def _state_response(state: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "state": state}


class SharedSettingsClient:
    def __init__(self) -> None:
        self.settings: dict[str, Any] = {
            "enabled": False,
            "allow_lan": False,
            "port": 18765,
        }

    def request_sync(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del timeout_seconds
        assert command in {
            "local_ai_mobile_get_investment_snapshot",
            "local_ai_mobile_submit_investment_instruction",
        }
        if payload.get("operation") == "update_shared_settings":
            self.settings.update(dict(payload["settings"]))
        return {"ok": True, "sync": dict(self.settings)}


def test_investment_manager_persists_mobile_settings_in_shared_store() -> None:
    service = MobileBridgeHarness()
    result = asyncio.run(
        service._investment_mobile_submit_instruction(
            {
                "operation": "update_shared_settings",
                "settings": {"enabled": True, "allow_lan": True, "port": 19001},
            }
        )
    )

    assert result["ok"] is True
    assert result["sync"]["enabled"] is True
    assert result["sync"]["allow_lan"] is True
    assert result["sync"]["port"] == 19001
    assert service.analytics_store.values == {
        "mobile_sync_enabled": True,
        "mobile_sync_allow_lan": True,
        "mobile_sync_port": 19001,
    }


def test_mobile_runtime_has_no_separate_repository(tmp_path: Path) -> None:
    service = InvestmentMobileService(tmp_path)
    client = SharedSettingsClient()
    service._client = client

    status = service.status()

    assert status["main_system_independent_tool"] is True
    assert status["business_layer_owner"] == "ai-assistant"
    assert status["settings_owner"] == "ai-assistant"
    assert status["cache_owner"] == "ai-assistant"
    assert status["cache_storage"] == (
        "ai-assistant/runtime/cache/companions/investment-mobile"
    )
    assert status["backup_owner"] == "ai-assistant"
    assert status["backup_storage"] == (
        "global-cleaner/data/business/backups/ai-assistant"
    )
    assert status["separate_business_layer"] is False
    assert status["separate_settings_layer"] is False
    assert status["database"] == "ai-assistant-shared-repository"
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_mobile_runtime_updates_settings_through_shared_route(tmp_path: Path) -> None:
    service = InvestmentMobileService(tmp_path)
    client = SharedSettingsClient()
    service._client = client

    _, result = asyncio.run(service.handle("investment_mobile_stop", {}))

    assert result["shared_settings"]["enabled"] is False
    assert client.settings["enabled"] is False
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_ai_assistant_channel_imports_canonical_application_service() -> None:
    source = (TOOL_ROOT / "src" / "channel_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "from ai_nexus.application.service import AiNexusService" in source
    assert "from ai_nexus.service import AiNexusService" not in source
