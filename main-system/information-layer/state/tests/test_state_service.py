"""State service test suite.

Tests for the State service information-layer module.
"""
from __future__ import annotations

from pathlib import Path


def test_state_module_importable():
    """The state module package should be importable."""
    import importlib
    mod = importlib.import_module('state')
    assert mod is not None


def test_state_module_init_exists():
    """The state module should have an __init__.py."""
    module_root = Path(__file__).resolve().parents[1]
    init_file = module_root / '__init__.py'
    assert init_file.is_file(), 'state __init__.py missing'
