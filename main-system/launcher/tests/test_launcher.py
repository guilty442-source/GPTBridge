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
