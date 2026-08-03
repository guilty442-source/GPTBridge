from __future__ import annotations

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
        "shared-layer/data/system-channel.sqlite3",
    )
    assert ai_submit.grants[0].path_roots == (
        "shared-layer/data/ai-channel.sqlite3",
    )


def test_ai_channel_permissions_are_participant_scoped() -> None:
    bindings = {
        item.actor: set(item.capabilities)
        for item in identity_permission_snapshot()
    }
    assert "ai-channel-request-process" in bindings["governance/tool/local-ai"]
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
    programming = by_name["star-project-programming"]
    database = by_name["star-investment-manager-database-read"]
    project_databases = by_name["star-project-database-operations"]

    assert {grant.action for grant in programming.grants} == {
        "append",
        "check",
        "delete",
        "diagnose",
        "execute",
        "read",
        "read-source",
        "repair",
        "rollback",
        "update",
        "verify",
        "write",
    }
    assert all("governance_rule" in grant.excluded_path_roots for grant in programming.grants)
    assert [(grant.action, grant.target) for grant in database.grants] == [
        ("read", "tool-business-storage:ai-assistant")
    ]
    assert {grant.action for grant in project_databases.grants} == {
        "append",
        "delete",
        "execute",
        "read",
        "rollback",
        "update",
        "write",
    }
    assert all(
        "governance_rule" in grant.excluded_path_roots
        for grant in project_databases.grants
    )
