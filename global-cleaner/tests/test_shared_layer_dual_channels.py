from __future__ import annotations

import json
from pathlib import Path

from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)


def test_system_and_ai_channels_have_separate_capabilities_and_databases() -> None:
    capabilities, _repairs = capability_boundary_snapshot()
    by_name = {item.capability: item for item in capabilities}

    system_submit = by_name["system-channel-request-submit"]
    ai_submit = by_name["ai-channel-request-submit"]
    assert system_submit.grants[0].path_roots == (
        "postgresql:gptbridge_transport:system",
    )
    assert ai_submit.grants[0].path_roots == (
        "postgresql:gptbridge_transport:ai",
    )


def test_ai_channel_permissions_are_participant_scoped() -> None:
    bindings = {
        item.actor: set(item.capabilities)
        for item in identity_permission_snapshot()
    }
    assert "ai-channel-request-process" in bindings["governance/tool/xingcheng"]
    assert "ai-channel-request-process" in bindings["governance/tool/ai-assistant"]
    assert "ai-channel-request-process" in bindings[
        "governance/tool/ai-collaboration"
    ]
    assert "ai-channel-request-submit" in bindings[
        "governance/tool/investment-mobile"
    ]
    assert "ai-channel-request-process" not in bindings[
        "governance/tool/investment-mobile"
    ]
    assert "ai-channel-request-submit" not in bindings[
        "governance/tool/file-sorter"
    ]


def test_star_programming_excludes_governance_and_has_project_database_access() -> None:
    capabilities, _repairs = capability_boundary_snapshot()
    by_name = {item.capability: item for item in capabilities}
    database = by_name["star-investment-manager-database-read"]

    assert database.owner == "tool:xingcheng"
    assert [(grant.action, grant.target) for grant in database.grants] == [
        ("read", "tool-business-storage:ai-assistant"),
    ]
    assert database.grants[0].data_scope == "ai-assistant-investment-database"

    programming_capability = json.loads(
        (Path(__file__).resolve().parents[2] / "local-model" / "manifest.json").read_text(
            "utf-8"
        )
    )["capabilities"]["star-project-programming"]
    assert programming_capability["excluded_path_roots"] == ["governance_rule"]
