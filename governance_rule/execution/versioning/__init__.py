from __future__ import annotations

from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
from governance_rule.permission_directory.execution.path_guard import permission_denied
from governance_rule.governance_policy import governance_policy_snapshot


def validate_loaded_authority_version() -> int:
    governance = governance_policy_snapshot()
    directory = directory_authority_snapshot()
    current = directory.authority_version_policy.current_version
    if governance.authority_version != current:
        raise permission_denied()
    return current


def validate_authority_release_version(
    target_version: int | None,
) -> int:
    policy = directory_authority_snapshot().authority_version_policy
    if not isinstance(target_version, int) or isinstance(target_version, bool):
        raise permission_denied()
    if target_version <= policy.current_version:
        raise permission_denied()
    return target_version
