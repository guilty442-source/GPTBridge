"""IPC service test suite.

Tests for the IPC service information-layer module.
"""
from __future__ import annotations

from pathlib import Path


def test_ipc_module_importable():
    """The ipc module package should be importable."""
    import importlib
    mod = importlib.import_module('ipc')
    assert mod is not None


def test_ipc_module_init_exists():
    """The ipc module should have an __init__.py."""
    module_root = Path(__file__).resolve().parents[1]
    init_file = module_root / '__init__.py'
    assert init_file.is_file(), 'ipc __init__.py missing'
