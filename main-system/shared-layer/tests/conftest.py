"""Shared Layer test suite (TS_SHARED_LAYER) conftest.

Codex basis: test-suite-architecture-directory (A57/E43).
Owner: permission-sovereign.
Management owner: health-maintenance-test-sub-sovereign.

This suite re-exports tests from the actual location at
shared-layer/tests per the codex-registered path.
"""
from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
ACTUAL_SUITE = WORKSPACE_ROOT / 'shared-layer' / 'tests'

sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(ACTUAL_SUITE))
sys.path.insert(0, str(WORKSPACE_ROOT / 'shared-layer' / 'src'))
