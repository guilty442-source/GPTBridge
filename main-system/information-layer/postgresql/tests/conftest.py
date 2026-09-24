"""PostgreSQL authority adapter test suite conftest.

Codex basis: test-suite-architecture-directory (A57/E43).
Owner: permission-sovereign.
Management owner: decision-sovereign (A604; health-maintenance identity retired).
"""
from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[5]
MODULE_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(MODULE_ROOT))
