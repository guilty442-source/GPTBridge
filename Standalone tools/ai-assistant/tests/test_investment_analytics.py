"""ai-assistant consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

########################################################################
# source: ai-assistant/tests/test_investment_analytics.py
########################################################################
from pathlib import Path

from ai_nexus.infrastructure.analytics_repository import InvestmentAnalyticsStore


def test_analytics_database_persists_as_encrypted_valid_sqlite(tmp_path: Path) -> None:
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    store = InvestmentAnalyticsStore(tool_root)
    store.set_setting("base_currency", "TWD")
    assert store._database_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    database_path = store.database_path
    store.close()

    assert database_path.is_file()
    assert not database_path.read_bytes().startswith(b"SQLite format 3\x00")

    reopened = InvestmentAnalyticsStore(tool_root)
    try:
        assert reopened.get_setting("base_currency") == "TWD"
        assert reopened._database_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()
