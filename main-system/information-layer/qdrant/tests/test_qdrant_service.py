"""Qdrant authority adapter test suite.

Tests for the Qdrant authority adapter information-layer module.
"""
from __future__ import annotations

from pathlib import Path


def test_qdrant_module_importable():
    """The qdrant module package should be importable."""
    import importlib
    mod = importlib.import_module('qdrant')
    assert mod is not None


def test_qdrant_module_init_exists():
    """The qdrant module should have an __init__.py."""
    module_root = Path(__file__).resolve().parents[1]
    init_file = module_root / '__init__.py'
    assert init_file.is_file(), 'qdrant __init__.py missing'
