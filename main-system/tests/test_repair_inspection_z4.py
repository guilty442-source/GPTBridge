"""Z4 bounded SQLite repair inspection evidence."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import _main_system_test_support as _support  # noqa: F401

from tasks.repair_inspection import DatabaseRecoveryInspector


def test_z4_inspects_independent_databases_and_preserves_corruption(tmp_path: Path) -> None:
    """A610/A621: real SQLite files are retired residue requiring
    preservation; a non-database .sqlite3 file is inspected and checked."""
    target = tmp_path / "tool"
    database_root = target / "runtime" / "state"
    database_root.mkdir(parents=True)
    residue = database_root / "residue.sqlite3"
    with sqlite3.connect(residue) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.commit()
    stray = database_root / "stray.sqlite3"
    stray.write_bytes(b"not-a-sqlite-database")

    result = DatabaseRecoveryInspector(tmp_path, target).inspect()

    assert result["checked_databases"] == ["tool/runtime/state/stray.sqlite3"]
    assert result["backup_extract_paths"] == ["tool/runtime/state/residue.sqlite3"]
    assert result["preserved_databases"]
    assert result["database_errors"]
