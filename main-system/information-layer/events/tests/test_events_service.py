"""Event service test suite.

Tests for the Event service information-layer module.
"""
from __future__ import annotations

from pathlib import Path


def test_events_module_importable():
    """The events module package should be importable."""
    import importlib
    mod = importlib.import_module('events')
    assert mod is not None


def test_events_module_init_exists():
    """The events module should have an __init__.py."""
    module_root = Path(__file__).resolve().parents[1]
    init_file = module_root / '__init__.py'
    assert init_file.is_file(), 'events __init__.py missing'
