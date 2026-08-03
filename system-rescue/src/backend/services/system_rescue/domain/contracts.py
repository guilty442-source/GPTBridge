from __future__ import annotations

from typing import Final


SYSTEM_RESCUE_VERSION: Final[str] = "1.0.0"
REQUIRED_PATHS: Final[tuple[str, ...]] = (
    "governance_rule/governance_policy.py",
    "governance_rule/code_rule_directory.py",
    "governance_rule/execution/authentication/__init__.py",
    "governance_rule/permission_directory/directory_authority.py",
    "governance_rule/permission_directory/execution/identity_registry/__init__.py",
    "main-system/src-core/main.py",
    "main-system/src-core/ipc/server.py",
    "main-system/package.json",
)


__all__ = ["REQUIRED_PATHS", "SYSTEM_RESCUE_VERSION"]
