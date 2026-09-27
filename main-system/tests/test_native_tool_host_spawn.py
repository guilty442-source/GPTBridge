"""Contract tests for the migrate-csharp native tool-host spawn path.

Pins the design contract in docs/csharp-tool-host-design.md:
- manifest ``runtime.native_entry`` selects a tool-root .exe host,
- the spawn layer execs the exe directly (no interpreter argv prefix),
- process discovery matches a native runtime entry by ExecutablePath.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = WORKSPACE_ROOT / "Standalone tools" / "system-rescue"
SRC_CORE = WORKSPACE_ROOT / "main-system" / "src-core"


def _resolver():
    from tasks.tool_path_resolver import ToolPathResolver

    return ToolPathResolver(WORKSPACE_ROOT)


def test_system_rescue_manifest_declares_native_entry():
    manifest = json.loads((TOOL_DIR / "manifest.json").read_text("utf-8"))
    native = manifest["runtime"].get("native_entry")
    assert native == "dist/SystemRescue.Host.exe"
    # Python runtime stays the declared fallback during migration.
    assert manifest["runtime"]["type"] == "python"
    assert manifest["runtime"]["entry"] == "src/main.py"


def test_resolver_prefers_native_exe_when_present():
    manifest = json.loads((TOOL_DIR / "manifest.json").read_text("utf-8"))
    resolver = _resolver()
    entry = resolver.resolve_special_unpacked_entry(manifest, TOOL_DIR)
    if (TOOL_DIR / "dist" / "SystemRescue.Host.exe").is_file():
        assert entry.suffix.lower() == ".exe"
        assert str(entry).endswith("SystemRescue.Host.exe")
    else:
        # Artifact not built on this machine → governed fallback.
        assert entry.suffix == ".py"


def test_resolver_rejects_native_entry_escape():
    manifest = {
        "runtime": {"type": "python", "native_entry": "..\\evil.exe"},
        "request_channel": {
            "model": "governance-authenticated-shared-layer",
            "direct_instruction": "PERMISSION_DENIED",
            "runtime_entry": "src/channel_runtime.py",
        },
        "launch": {"background": "governed-source-channel"},
        "distribution": {"mode": "special-unpackaged", "package": False},
    }
    with pytest.raises(ValueError):
        _resolver().resolve_special_unpacked_entry(manifest, TOOL_DIR)


def test_resolver_rejects_non_exe_native_entry(tmp_path):
    fake = tmp_path / "payload.py"
    fake.write_text("print(1)", encoding="utf-8")
    manifest = {
        "runtime": {"type": "python", "native_entry": "src/channel_runtime.py"},
        "request_channel": {
            "model": "governance-authenticated-shared-layer",
            "direct_instruction": "PERMISSION_DENIED",
            "runtime_entry": "src/channel_runtime.py",
        },
        "launch": {"background": "governed-source-channel"},
        "distribution": {"mode": "special-unpackaged", "package": False},
    }
    with pytest.raises(ValueError):
        _resolver().resolve_special_unpacked_entry(manifest, TOOL_DIR)


def test_spawn_branches_on_exe_entry():
    source = (SRC_CORE / "tasks" / "toolbox_start_spawn_process.py").read_text(
        encoding="utf-8"
    )
    assert 'source_entry.suffix.lower() == ".exe"' in source
    assert "spawn_argv" in source


def test_registry_matches_native_exe_by_image_path():
    source = (SRC_CORE / "tasks" / "tool_process_registry.py").read_text(
        encoding="utf-8"
    )
    assert 'source_runtime_entry.lower().endswith(".exe")' in source
    assert "ExecutablePath" in source
