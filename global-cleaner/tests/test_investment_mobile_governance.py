from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from governance_rule.permission_directory.registries.permissions.tool_routes import (
    authorize_ai_route,
    authorize_investment_mobile_route,
)


def test_mobile_connects_to_star_not_directly_to_investment_manager() -> None:
    authorize_investment_mobile_route(
        "governance/tool/investment-mobile",
        "local-ai",
        "local_ai_mobile_get_investment_snapshot",
    )
    with pytest.raises(PermissionError):
        authorize_investment_mobile_route(
            "governance/tool/investment-mobile",
            "ai-assistant",
            "investment_mobile_get_snapshot",
        )


def test_only_star_can_proxy_mobile_commands_to_investment_manager() -> None:
    assert (
        authorize_ai_route(
            "governance/tool/local-ai",
            "ai-assistant",
            "investment_mobile_get_snapshot",
        )
        == "local-ai"
    )
    with pytest.raises(PermissionError):
        authorize_ai_route(
            "governance/tool/investment-mobile",
            "ai-assistant",
            "investment_mobile_get_snapshot",
        )


def test_investment_manager_process_network_policy_blocks_external_hosts() -> None:
    module_path = ROOT / "ai-assistant" / "src" / "investment_network_policy.py"
    spec = importlib.util.spec_from_file_location("investment_network_policy_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_investment_manager_network_policy()

    with pytest.raises(PermissionError):
        socket.getaddrinfo("example.com", 443)
    assert socket.getaddrinfo("127.0.0.1", 80)
