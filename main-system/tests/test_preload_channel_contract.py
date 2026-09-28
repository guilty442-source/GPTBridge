"""G84 invoke channel contract — every allowlisted channel must have a dispatch arm.

A621: the Electron preload/ipcMain pair is retired.  The Rust/Tauri shell
injects a preload shim that routes ``window.electron.invoke`` /
``window.gptBridge`` calls through the single ``gptbridge_invoke`` command,
which re-checks the whitelist server-side:

- main window: ``ALLOWED_CHANNELS`` (src-tauri/src/js_bridge/mod.rs)
  dispatched by ``js_bridge::channels``;
- tool window: ``TOOL_ALLOWED_CHANNELS`` (src-tauri/src/tool_dispatch.rs)
  dispatched by ``tool_dispatch::dispatch``.

A channel listed in a whitelist but missing a ``match`` arm drifts silently:
renderer calls fail at runtime with no test catching it.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC_TAURI = (
    Path(__file__).resolve().parents[1] / "src-tauri" / "src"
)

_ALLOWLIST_RE = re.compile(r'"([a-z0-9:\-]+)"', re.IGNORECASE)
_ARM_RE = re.compile(r'"([a-z0-9:\-]+)"\s*=>', re.IGNORECASE)


def _allowlist(source: Path, const_name: str) -> set[str]:
    text = source.read_text(encoding="utf-8-sig")
    block = text.split(const_name, 1)[1]
    entries = block.split("];", 1)[0]
    return set(_ALLOWLIST_RE.findall(entries))


def _dispatch_arms(source: Path) -> set[str]:
    return set(_ARM_RE.findall(source.read_text(encoding="utf-8-sig")))


_MAIN_BRIDGE = SRC_TAURI / "js_bridge" / "mod.rs"
_MAIN_DISPATCH = SRC_TAURI / "js_bridge" / "channels.rs"


def test_main_window_allowlist_has_dispatch_arms() -> None:
    missing = _allowlist(_MAIN_BRIDGE, "ALLOWED_CHANNELS") - _dispatch_arms(
        _MAIN_DISPATCH
    )
    assert not missing, f"ALLOWED_CHANNELS without dispatch arm: {sorted(missing)}"


def test_tool_window_allowlist_has_dispatch_arms() -> None:
    dispatch = SRC_TAURI / "tool_dispatch.rs"
    missing = _allowlist(dispatch, "TOOL_ALLOWED_CHANNELS") - _dispatch_arms(dispatch)
    assert not missing, f"TOOL_ALLOWED_CHANNELS without dispatch arm: {sorted(missing)}"


def test_gptbridge_surface_uses_registered_channels() -> None:
    """Channels invoked by the `gptBridge` shim must be allowlisted."""
    text = _MAIN_BRIDGE.read_text(encoding="utf-8-sig")
    shim = text.split("window.gptBridge", 1)[1]
    invoked = {c for c in _ALLOWLIST_RE.findall(shim.split("};", 1)[0]) if ":" in c}
    missing = invoked - _allowlist(_MAIN_BRIDGE, "ALLOWED_CHANNELS")
    assert not missing, f"gptBridge invokes non-allowlisted channels: {sorted(missing)}"


def test_no_dangling_backend_only_channel_in_allowlist() -> None:
    """Backend command-router channels must not sit in the invoke allowlist."""
    assert "app:get-repair-status" not in _allowlist(
        _MAIN_BRIDGE, "ALLOWED_CHANNELS"
    )