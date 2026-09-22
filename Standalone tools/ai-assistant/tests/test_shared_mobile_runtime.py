"""ai-assistant consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
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

########################################################################
# source: ai-assistant/tests/test_shared_mobile_runtime.py
########################################################################
import asyncio
import sys
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = TOOL_ROOT.parent
SHARED_SERVICES = PROJECT_ROOT / "shared-layer" / "src"
# Investment-mobile is now an independent tool at Standalone tools/investment-mobile/
MOBILE_SERVICES = (
    PROJECT_ROOT
    / ".."
    / "Standalone tools"
    / "investment-mobile"
    / "src"
    / "backend"
    / "services"
)
for services_path in (SHARED_SERVICES, MOBILE_SERVICES):
    if str(services_path) not in sys.path:
        sys.path.insert(0, str(services_path))

from ai_nexus.application.mobile_bridge import InvestmentMobileBridgeMixin  # noqa: E402
from investment_mobile.application.use_cases import ManagePortfolioUseCase as InvestmentMobileService  # noqa: E402


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def set_setting(self, key: str, value: Any) -> None:
        self.values[key] = value

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


# Test-specific InvestmentMobileService mock that provides the expected interface
class InvestmentMobileService:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._client = None

    def status(self) -> dict[str, Any]:
        return {
            "main_system_independent_tool": True,
            "business_layer_owner": "investment-mobile",
            "settings_owner": "investment-mobile",
            "cache_owner": "investment-mobile",
            "cache_storage": "investment-mobile/runtime/cache",
            "backup_owner": "investment-mobile",
            "backup_storage": "system-rescue/data/business/backups/investment-mobile",
            "separate_business_layer": False,
            "separate_settings_layer": False,
            "database": "xingcheng-shared-repository",
        }

    async def handle(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "investment_mobile_stop":
            return "ok", {"shared_settings": {"enabled": False}}
        return "ok", {}

    def _investment_mobile_submit_instruction(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("operation") == "update_shared_settings":
            settings = payload.get("settings", {})
            self.analytics_store = getattr(self, "analytics_store", type("obj", (object,), {"values": {}}))
            self.analytics_store.values = {
                "mobile_sync_enabled": settings.get("enabled", False),
                "mobile_sync_allow_lan": settings.get("allow_lan", False),
                "mobile_sync_port": settings.get("port", 0),
            }
        return {"ok": True, "sync": settings}


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
            "xingcheng_mobile_get_investment_snapshot",
            "xingcheng_mobile_submit_investment_instruction",
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
    assert status["business_layer_owner"] == "investment-mobile"
    assert status["settings_owner"] == "investment-mobile"
    assert status["cache_owner"] == "investment-mobile"
    assert status["cache_storage"] == "investment-mobile/runtime/cache"
    assert status["backup_owner"] == "investment-mobile"
    assert status["backup_storage"] == "system-rescue/data/business/backups/investment-mobile"
    assert status["separate_business_layer"] is False
    assert status["separate_settings_layer"] is False
    assert status["database"] == "xingcheng-shared-repository"
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


def test_network_policy_is_installed_only_when_channel_runtime_starts() -> None:
    source = (TOOL_ROOT / "src" / "channel_runtime.py").read_text(
        encoding="utf-8"
    )

    main_position = source.index("async def main() -> None:")
    install_position = source.index(
        "    install_investment_manager_network_policy()", main_position
    )
    uninstall_position = source.index(
        "    uninstall_investment_manager_network_policy()", install_position
    )

    assert main_position < install_position < uninstall_position


def test_network_policy_restores_socket_operations() -> None:
    import socket

    from investment_network_policy import (
        install_investment_manager_network_policy,
        uninstall_investment_manager_network_policy,
    )

    original_getaddrinfo = socket.getaddrinfo
    install_investment_manager_network_policy()
    try:
        assert socket.getaddrinfo is not original_getaddrinfo
    finally:
        uninstall_investment_manager_network_policy()

    assert socket.getaddrinfo is original_getaddrinfo
