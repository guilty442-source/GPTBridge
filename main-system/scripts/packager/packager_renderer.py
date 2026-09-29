# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from packager_base import (
    MAIN_SYSTEM_ROOT,
    PLATFORM_RENDERER_ROOT,
)
from renderer_build import build_tool_renderer


def renderer_output_dir(tool_id: str) -> Path:
    return PLATFORM_RENDERER_ROOT / tool_id / "renderer"


def build_platform_renderer(
    tool_id: str,
    tool_dir: Path | None = None,
) -> dict[str, Any]:
    """Build a tool renderer through the governed native chain
    (standalone SWC transform -> ESM -> esbuild bundle, B168/E35).
    Replaces the retired npx/vite invocation; no Node.js runtime is
    involved at any stage."""
    ui_root = (
        (tool_dir / "src" / "ui").resolve()
        if tool_dir is not None
        else None
    )
    if ui_root is None or not (ui_root / "index.html").is_file():
        return {
            "ok": False,
            "tool_id": tool_id,
            "renderer_path": str(renderer_output_dir(tool_id)),
            "exit_code": 2,
            "output": f"missing src/ui/index.html for {tool_id}",
        }
    env_tool_root = os.environ.get("GPTBRIDGE_PLATFORM_TOOL_ROOT")
    result = build_tool_renderer(tool_id, ui_root, renderer_output_dir(tool_id))
    index_path = renderer_output_dir(tool_id) / "index.html"
    return {
        "ok": bool(result.get("ok")) and index_path.exists(),
        "tool_id": tool_id,
        "renderer_path": str(renderer_output_dir(tool_id)),
        "exit_code": 0 if result.get("ok") else 2,
        "output": result.get("message") or (
            f"native build ok (tool_root={env_tool_root or tool_dir})"
        ),
    }
