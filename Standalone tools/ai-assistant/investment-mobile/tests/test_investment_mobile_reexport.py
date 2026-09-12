"""Re-export investment-mobile tests at the codex-registered path.

The canonical implementation lives at Standalone tools/investment-mobile/tests.
This file ensures the codex path is also a valid, runnable test root.
"""
from __future__ import annotations

import importlib
from pathlib import Path


_ACTUAL = Path(__file__).resolve().parents[4] / 'investment-mobile' / 'tests'


def test_investment_mobile_module_importable():
    """The investment-mobile test module should be importable."""
    spec = importlib.util.spec_from_file_location(
        'test_investment_mobile',
        _ACTUAL / 'test_investment_mobile.py',
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod is not None


def test_actual_suite_root_has_tests():
    """The actual investment-mobile tests directory should exist."""
    assert _ACTUAL.is_dir(), f'Actual suite missing: {_ACTUAL}'
    test_files = list(_ACTUAL.glob('test_*.py'))
    assert len(test_files) > 0, 'No test files in actual suite'
