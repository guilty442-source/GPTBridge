"""Toolbox launch helpers (A185 split).

Contains the runtime readiness check and environment setup helpers
extracted from _launch_source_ui.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tool_codenames import get_tool_codename


def _check_source_runtime_ready(
    runtime_port: int,
    expected_runtime_tool_id: str,
    workspace_instance_id: str,
) -> bool:
    """Check if the source runtime is ready by polling its health endpoint."""
    if not 1024 <= runtime_port <= 65535:
        return False
    try:
        _opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        with _opener.open(
            urllib.request.Request(
                f"http://127.0.0.1:{runtime_port}/health",
                headers={"Connection": "close"},
            ),
            timeout=0.75,
        ) as response:
            payload = json.loads(response.read(65_537).decode("utf-8"))
        return bool(
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("governance_ready") is True
            and str(payload.get("tool_id") or "")
            == expected_runtime_tool_id
            and str(payload.get("workspace_instance_id") or "")
            == workspace_instance_id
        )
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return False


def _native_ui_binary(project_root: Path, tool_id: str) -> Path | None:
    """Registered native window surface for the tool (E180/C116).

    ``config/native-ui-surfaces.json`` lists tools whose window is served
    by a native Rust binary under the same ``--tool-window`` contract;
    every other tool keeps the Tauri WebView2 renderer.
    """
    registry = (
        project_root / "main-system" / "config" / "native-ui-surfaces.json"
    )
    try:
        surfaces = json.loads(registry.read_text(encoding="utf-8")).get(
            "surfaces"
        ) or {}
        binary = str(
            (surfaces.get(tool_id) or {}).get("binary") or ""
        ).strip()
    except (OSError, ValueError):
        return None
    if not binary:
        return None
    target = project_root / "main-system" / "src-tauri" / "target"
    for profile in ("release", "debug"):
        candidate = target / profile / binary
        if candidate.is_file():
            return candidate.resolve()
    return None


def _resolve_source_ui_paths(
    project_root: Path,
    tool_id: str,
) -> dict[str, Path]:
    """Resolve the renderer entry and the governed shell binary path.

    Returns a dict with keys: shell, and renderer_entry when the tool is
    still served by the WebView2 renderer (tools with a registered native
    surface carry no renderer entry).

    A618/A621/A625: the Rust/Tauri shell (``gptbridge-shell.exe
    --tool-window``) replaces the retired Electron host; the release build
    is canonical and the debug build is accepted as a development
    fallback so a source checkout without ``--release`` still opens tool
    windows.
    """
    native = _native_ui_binary(project_root, tool_id)
    if native is not None:
        return {"shell": native}
    renderer_entry = (
        project_root
        / "main-system"
        / "dist-ui"
        / "independent-tools"
        / tool_id
        / "renderer"
        / "index.html"
    ).resolve()
    tauri_target = (
        project_root / "main-system" / "src-tauri" / "target"
    )
    shell = tauri_target / "release" / "gptbridge-shell.exe"
    if not shell.is_file():
        shell = tauri_target / "debug" / "gptbridge-shell.exe"
    shell = shell.resolve()
    return {
        "renderer_entry": renderer_entry,
        "shell": shell,
    }


def _build_source_ui_environment(
    environment: dict[str, str],
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
    expected_runtime_tool_id: str,
    runtime_environment: dict[str, str],
    renderer_entry: Path,
    project_root: Path,
    workspace_instance_id: str,
) -> dict[str, str]:
    """Build the environment for the source UI subprocess."""
    window = manifest.get("window") if isinstance(manifest.get("window"), dict) else {}
    environment.update(
        {
            "GPTBRIDGE_SOURCE_UI_TOOL_ID": tool_id,
            "GPTBRIDGE_SOURCE_UI_WORKSPACE_ROOT": str(project_root.resolve()),
            "GPTBRIDGE_SOURCE_UI_TOOL_ROOT": str(tool_dir.resolve()),
            "GPTBRIDGE_SOURCE_UI_RENDERER_ENTRY": str(renderer_entry),
            "GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL": (
                f"ws://127.0.0.1:{runtime_environment['GPTBRIDGE_IPC_PORT']}/"
                f"?token={runtime_environment['GPTBRIDGE_IPC_SESSION_TOKEN']}"
                f"&instance={workspace_instance_id}"
            ),
            "GPTBRIDGE_SOURCE_UI_CODENAME": get_tool_codename(tool_id),
            "GPTBRIDGE_SOURCE_UI_TITLE": str(
                manifest.get("display_name") or tool_id
            ),
            "GPTBRIDGE_SOURCE_UI_WIDTH": str(window.get("width") or 1440),
            "GPTBRIDGE_SOURCE_UI_HEIGHT": str(window.get("height") or 920),
            "GPTBRIDGE_SOURCE_UI_MIN_WIDTH": str(window.get("minWidth") or 1120),
            "GPTBRIDGE_SOURCE_UI_MIN_HEIGHT": str(window.get("minHeight") or 760),
        }
    )
    return environment


__all__ = [
    "_check_source_runtime_ready",
    "_resolve_source_ui_paths",
    "_build_source_ui_environment",
]
