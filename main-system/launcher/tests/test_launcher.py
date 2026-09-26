"""Launcher test suite (TS_LAUNCHER).

Tests for the main-system launcher module (start.ps1, state management).
"""
from __future__ import annotations

from pathlib import Path


def test_launcher_state_dir_exists():
    """Launcher state directory should exist."""
    launcher_root = Path(__file__).resolve().parents[1]
    state_dir = launcher_root / 'state'
    assert state_dir.is_dir(), f'Launcher state dir missing: {state_dir}'


def test_launcher_scripts_dir_exists():
    """Launcher scripts directory should exist."""
    launcher_root = Path(__file__).resolve().parents[1]
    scripts_dir = launcher_root / 'scripts'
    assert scripts_dir.is_dir(), f'Launcher scripts dir missing: {scripts_dir}'


def test_launcher_src_dir_exists():
    """Launcher src directory should exist."""
    launcher_root = Path(__file__).resolve().parents[1]
    src_dir = launcher_root / 'src'
    assert src_dir.is_dir(), f'Launcher src dir missing: {src_dir}'


def test_bootstrap_entry_source_exists():
    """C# bootstrap entry (migrate-csharp owner) should be present."""
    launcher_root = Path(__file__).resolve().parents[1]
    project = launcher_root / 'src' / 'GPTBridge.Bootstrap'
    assert (project / 'Program.cs').is_file(), 'Program.cs missing'
    assert (project / 'GPTBridge.Bootstrap.csproj').is_file(), 'csproj missing'


def test_bootstrap_csproj_targets_net10():
    """The bootstrap entry targets .NET 10 per the language baseline."""
    launcher_root = Path(__file__).resolve().parents[1]
    csproj = (launcher_root / 'src' / 'GPTBridge.Bootstrap'
              / 'GPTBridge.Bootstrap.csproj').read_text(encoding='utf-8')
    assert 'net10.0' in csproj


def test_desktop_stubs_prefer_bootstrap_entry():
    """Both desktop bootstrap stubs prefer the C# bootstrap entry."""
    launcher_root = Path(__file__).resolve().parents[1]
    for name in ('GPTBridgeLauncher.cpp', 'GPTBridgeLauncher.cs'):
        text = (launcher_root / 'src' / name).read_text(encoding='utf-8',
                                                        errors='replace')
        assert 'GPTBridge.Bootstrap.exe' in text, (
            f'{name} does not reference the bootstrap entry')


def test_install_publishes_bootstrap_entry():
    """install.py republishes the C# bootstrap entry."""
    launcher_root = Path(__file__).resolve().parents[1]
    text = (launcher_root / 'scripts' / 'install.py').read_text(
        encoding='utf-8', errors='replace')
    assert 'publish_bootstrap_entry' in text
    assert 'GPTBridge.Bootstrap.exe' in text


def test_refresh_sources_cover_bootstrap_entry():
    """Refresh fingerprint lists must cover the bootstrap entry sources."""
    launcher_root = Path(__file__).resolve().parents[1]
    for script in ('install.py', 'start.py'):
        text = (launcher_root / 'scripts' / script).read_text(
            encoding='utf-8', errors='replace')
        assert 'src/GPTBridge.Bootstrap/Program.cs' in text, (
            f'{script} LAUNCHER_BUILD_SOURCES missing bootstrap entry')