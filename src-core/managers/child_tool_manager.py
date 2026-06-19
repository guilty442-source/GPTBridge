from __future__ import annotations

from pathlib import Path
from typing import Any

from .child_tool_workspace import ChildToolWorkspace, sanitize_tool_name


async def generate_child_tool_action(app: Any, tool_name: str) -> dict[str, object]:
    """Legacy entry point kept for compatibility with older commands."""
    project_root = Path(__file__).resolve().parent.parent.parent
    workspace = ChildToolWorkspace(project_root)
    sanitized = sanitize_tool_name(tool_name)
    project = workspace.create_project(sanitized, "desktop_tool")
    package = await workspace.package_project(sanitized)
    if getattr(app, "history_manager", None):
        app.history_manager.record(
            f"[design] windows 11 child tool package result={'OK' if package.get('ok') else 'FAILED'}, tool={sanitized}"
        )
    return {
        **package,
        "project": project,
        "message": package.get("message", "child tool package completed"),
    }
