"""Tool codenames mapping for environment variables.

This module provides a central mapping from tool IDs to their codenames
as defined in the identity registry. Using codenames instead of version
numbers in environment variables provides stable identifiers that don't
change with version updates.
"""

from __future__ import annotations

from typing import Final

# Codename mapping per identity registry (governance_rule/permission_directory/registries/permissions/identity_groups.py)
TOOL_CODENAMES: Final[dict[str, str]] = {
    "main-system": "CENTRAL",
    "governance_rule": "SOVEREIGN",
    "shared-layer": "CONDUIT",
    "global-cleaner": "SWEEPER",
    "ai-assistant": "STEWARD",
    "ai-collaboration": "ENVOY",
    "star-chat": "DIALOGUE",
    "file-sorter": "SORTER",
    "xingcheng": "NEBULA",
    "investment-mobile": "MOBILE",
    "vaultly": "VAULT",
    "system-rescue": "RESCUE",
}


def get_tool_codename(tool_id: str) -> str:
    """Get the codename for a tool ID.

    Args:
        tool_id: The tool identifier (e.g., "ai-assistant")

    Returns:
        The codename (e.g., "STEWARD") or the tool_id in uppercase if not found.
    """
    return TOOL_CODENAMES.get(tool_id, tool_id.upper())


def get_tool_id_by_codename(codename: str) -> str | None:
    """Get the tool ID for a codename.

    Args:
        codename: The codename (e.g., "STEWARD")

    Returns:
        The tool_id (e.g., "ai-assistant") or None if not found.
    """
    for tool_id, cn in TOOL_CODENAMES.items():
        if cn == codename:
            return tool_id
    return None


__all__ = ["TOOL_CODENAMES", "get_tool_codename", "get_tool_id_by_codename"]