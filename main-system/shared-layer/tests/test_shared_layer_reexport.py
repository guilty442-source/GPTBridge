"""Re-export shared-layer tests at the codex-registered path.

The canonical implementation lives at shared-layer/tests.
This file ensures the codex path is also a valid, runnable test root.
"""
from __future__ import annotations

from pathlib import Path


_ACTUAL = Path(__file__).resolve().parents[3] / 'shared-layer' / 'tests'


def test_actual_suite_root_has_tests():
    """The actual shared-layer tests directory should exist."""
    assert _ACTUAL.is_dir(), f'Actual suite missing: {_ACTUAL}'
    test_files = list(_ACTUAL.glob('test_*.py'))
    assert len(test_files) > 0, 'No test files in actual suite'


def test_shared_layer_importable():
    """The shared_layer package should be importable."""
    import importlib
    mod = importlib.import_module('shared_layer')
    assert mod is not None
