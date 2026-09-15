"""global-cleaner consolidated test suite (A57/E43)

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
    # Own tool path last: insert(0) makes it win over other tools' `backend`
    # packages (several tools ship a top-level `backend` package).
    str(_ROOT / "Standalone tools" / "global-cleaner" / "src"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

########################################################################
# source: global-cleaner/tests/test_investment_mobile_governance.py
########################################################################
import importlib.util
import socket
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from governance_rule.permission_directory.registries.permissions.tool_routes import (
    authorize_ai_route,
    authorize_investment_mobile_route,
)


def test_mobile_connects_to_star_not_directly_to_investment_manager() -> None:
    authorize_investment_mobile_route(
        "governance/tool/investment-mobile",
        "xingcheng",
        "xingcheng_mobile_get_investment_snapshot",
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
            "governance/tool/xingcheng",
            "ai-assistant",
            "investment_mobile_get_snapshot",
        )
        == "xingcheng"
    )
    with pytest.raises(PermissionError):
        authorize_ai_route(
            "governance/tool/investment-mobile",
            "ai-assistant",
            "investment_mobile_get_snapshot",
        )


def test_investment_manager_process_network_policy_blocks_external_hosts() -> None:
    module_path = ROOT / "Standalone tools" / "ai-assistant" / "src" / "investment_network_policy.py"
    spec = importlib.util.spec_from_file_location("investment_network_policy_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_investment_manager_network_policy()
    try:
        with pytest.raises(PermissionError):
            socket.getaddrinfo("example.com", 443)
        assert socket.getaddrinfo("127.0.0.1", 80)
    finally:
        module.uninstall_investment_manager_network_policy()
