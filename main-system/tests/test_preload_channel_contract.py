"""G84 preload channel contract — every allowlisted invoke channel must have a Main handler.

`preload.ts` gates `ipcRenderer.invoke` behind `allowedInvokeChannels`; a channel
listed there but never registered via `ipcMain.handle` in `src-ui/main/` is a
drift: renderer calls fail at runtime with no test catching it.
"""
from __future__ import annotations

import re
from pathlib import Path

MAIN_DIR = Path(__file__).resolve().parents[1] / "src-ui" / "main"
PRELOAD = MAIN_DIR / "preload.ts"

_ALLOWLIST_RE = re.compile(r"'([a-z0-9:\-]+)'", re.IGNORECASE)
_HANDLE_RE = re.compile(r"ipcMain\.handle\s*\(\s*['\"]([a-z0-9:\-]+)['\"]", re.IGNORECASE)


def _allowlist() -> set[str]:
    text = PRELOAD.read_text(encoding="utf-8-sig")
    block = text.split("allowedInvokeChannels", 1)[1]
    entries = block.split("])", 1)[0]
    return set(_ALLOWLIST_RE.findall(entries))


def _registered_handlers() -> set[str]:
    channels: set[str] = set()
    for ts_file in MAIN_DIR.glob("*.ts"):
        channels.update(_HANDLE_RE.findall(ts_file.read_text(encoding="utf-8-sig")))
    return channels


def test_every_allowlisted_channel_has_main_handler() -> None:
    missing = _allowlist() - _registered_handlers()
    assert not missing, f"preload allowlist channels without ipcMain.handle: {sorted(missing)}"


def test_gptbridge_surface_uses_registered_channels() -> None:
    """Channels invoked by the `gptBridge` bridge must also be registered handlers."""
    text = PRELOAD.read_text(encoding="utf-8-sig")
    bridge_block = text.split("exposeInMainWorld('gptBridge'", 1)[1]
    invoked = set(_ALLOWLIST_RE.findall(bridge_block.split("})", 1)[0]))
    # filter to channel-like tokens (contain ':')
    invoked = {c for c in invoked if ":" in c}
    missing = invoked - _registered_handlers()
    assert not missing, f"gptBridge invokes channels without ipcMain.handle: {sorted(missing)}"


def test_no_dangling_backend_only_channel_in_allowlist() -> None:
    """Backend command-router channels must not sit in the Electron invoke allowlist."""
    assert "app:get-repair-status" not in _allowlist()
