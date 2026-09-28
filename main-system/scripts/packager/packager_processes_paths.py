"""Packager processes — path helpers.

Extracted from packager_processes.py: standalone project root,
backend owner path, legacy owner path, and workspace instance id.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def legacy_standalone_backend_owner_path(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        state_base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
        state_base = (
            Path(xdg_state_home)
            if xdg_state_home
            else Path.home() / ".local" / "state"
        )
    return state_base / "GPTBridge" / "ipc" / f"standalone-{safe_tool_id}-backend.json"


def standalone_project_root(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        state_base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    else:
        xdg_state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
        state_base = (
            Path(xdg_state_home)
            if xdg_state_home
            else Path.home() / ".local" / "state"
        )
    return Path(
        os.path.abspath(state_base / "GPTBridge" / "standalone" / safe_tool_id)
    )


def standalone_backend_owner_path(tool_id: str) -> Path:
    safe_tool_id = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in tool_id
    ) or "tool"
    return (
        standalone_project_root(tool_id)
        / "runtime"
        / "ipc"
        / f"standalone-{safe_tool_id}-backend.json"
    )


def workspace_instance_id(project_root: Path) -> str:
    normalized = os.path.normcase(
        os.path.abspath(project_root)
    ).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
