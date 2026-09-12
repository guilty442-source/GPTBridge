"""Launcher test suite (TS_LAUNCHER) — conftest.

Codex basis: test-suite-architecture-directory (A57/E43).
Owner: permission-sovereign.
Management owner: health-maintenance-test-sub-sovereign.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
LAUNCHER_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(LAUNCHER_ROOT))
sys.path.insert(0, str(LAUNCHER_ROOT / 'src'))
