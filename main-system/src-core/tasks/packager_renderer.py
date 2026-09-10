# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from packager_base import (
    MAIN_SYSTEM_ROOT,
    PLATFORM_RENDERER_ROOT,
    TEMPLATE_DIR,
    _background_subprocess_kwargs,
)


def npx_command() -> str:
    return "npx.cmd" if os.name == "nt" else "npx"


def renderer_output_dir(tool_id: str) -> Path:
    return PLATFORM_RENDERER_ROOT / tool_id / "renderer"


def build_platform_renderer(
    tool_id: str,
    tool_dir: Path | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["GPTBRIDGE_PLATFORM_TOOL_ID"] = tool_id
    if tool_dir is not None:
        env["GPTBRIDGE_PLATFORM_TOOL_ROOT"] = str(tool_dir.resolve())
    command = [
        npx_command(),
        "vite",
        "build",
        "-c",
        "vite.platform-tools.config.ts",
    ]
    completed = subprocess.run(
        command,
        cwd=str(MAIN_SYSTEM_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **_background_subprocess_kwargs(),
    )
    output_dir = renderer_output_dir(tool_id)
    index_path = output_dir / "index.html"
    if completed.returncode == 0 and not index_path.exists():
        html_files = list(output_dir.rglob("*.html"))
        if len(html_files) == 1:
            html_source = html_files[0]
            html = html_source.read_text(encoding="utf-8")
            relative_prefix = "../" * len(html_source.relative_to(output_dir).parents[:-1])
            if relative_prefix:
                html = html.replace(f'{relative_prefix}assets/', './assets/')
            index_path.write_text(html, encoding="utf-8", newline="\n")
            html_source.unlink()
            for parent in reversed(html_source.relative_to(output_dir).parents[:-1]):
                candidate = output_dir / parent
                if candidate.exists() and not any(candidate.iterdir()):
                    candidate.rmdir()
    return {
        "ok": completed.returncode == 0 and index_path.exists(),
        "tool_id": tool_id,
        "renderer_path": str(output_dir),
        "exit_code": completed.returncode,
        "output": completed.stdout,
    }


def copy_app_templates(app_dir: Path) -> None:
    for filename in ("main.cjs", "preload.cjs"):
        source = TEMPLATE_DIR / filename
        if not source.exists():
            raise FileNotFoundError(f"Wrapper template not found: {source}")
        shutil.copy2(source, app_dir / filename)
