from __future__ import annotations

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
