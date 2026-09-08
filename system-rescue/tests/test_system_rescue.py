"""system-rescue consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "global-cleaner" / "src"),
    str(_ROOT / "ai-assistant" / "src"),
    str(_ROOT / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

import ast
import json


def test_system_rescue_manifest_declares_governed_runtime() -> None:
    tool_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((tool_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "system-rescue"
    assert manifest["request_channel"]["runtime_entry"] == "src/channel_runtime.py"
    assert manifest["test_targets"] == ["tests/test_system_rescue.py"]
    assert (tool_root / manifest["request_channel"]["runtime_entry"]).is_file()


def test_system_rescue_runtime_entries_parse_and_share_one_channel() -> None:
    tool_root = Path(__file__).resolve().parents[1]
    main_source = (tool_root / "src" / "main.py").read_text(encoding="utf-8")
    channel_source = (tool_root / "src" / "channel_runtime.py").read_text(encoding="utf-8")
    ast.parse(main_source)
    ast.parse(channel_source)
    assert "from channel_runtime import main as _channel_main" in main_source
    assert 'ROOT / "shared-layer" / "src"' in channel_source
