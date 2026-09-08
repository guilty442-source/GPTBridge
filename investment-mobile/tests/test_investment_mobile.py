"""investment-mobile consolidated test suite (A57/E43)

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


def test_investment_mobile_manifest_targets_owned_runtime_and_test() -> None:
    tool_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((tool_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "investment-mobile"
    assert manifest["test_targets"] == ["tests/test_investment_mobile.py"]
    runtime_entry = tool_root / manifest["request_channel"]["runtime_entry"]
    assert runtime_entry.is_file()
    ast.parse(runtime_entry.read_text(encoding="utf-8"))


def test_investment_mobile_runtime_is_non_resident_and_governed() -> None:
    tool_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((tool_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["lifecycle"]["startup"] == "on-demand"
    assert manifest["request_channel"]["model"] == "governance-authenticated-shared-layer"
    assert manifest["request_channel"]["direct_instruction"] == "PERMISSION_DENIED"
