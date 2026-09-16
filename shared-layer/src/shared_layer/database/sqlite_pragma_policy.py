"""SQLite PRAGMA Policy (E7).

Central Python policy for SQLite PRAGMA settings.  Not every module
sets its own — this is the single source of truth.

Class A: governance codex / high-integrity → conservative
Class B: module-private formal state → normal
Class C: runtime / checkpoint → normal
Class D: cache / fallback → relaxed

Usage:
    from shared_layer.database.sqlite_pragma_policy import apply_pragma

    conn = sqlite3.connect(path)
    apply_pragma(conn, db_class="B")

Codex basis:
    A8/E21  — SQLite: owner-private-operational-state.
    A44/E30 — four-functions-local.
"""
from __future__ import annotations

import sqlite3
from typing import Any

# ============================================================================
# PRAGMA policies per class
# ============================================================================
_PRAGMA_POLICIES: dict[str, dict[str, str]] = {
    # Class A: governance codex / high-integrity
    "A": {
        "journal_mode": "WAL",
        "foreign_keys": "ON",
        "busy_timeout": "10000",
        "synchronous": "FULL",
        "wal_autocheckpoint": "1000",
    },
    # Class B: module-private formal state
    "B": {
        "journal_mode": "WAL",
        "foreign_keys": "ON",
        "busy_timeout": "5000",
        "synchronous": "NORMAL",
        "wal_autocheckpoint": "1000",
    },
    # Class C: runtime / checkpoint
    "C": {
        "journal_mode": "WAL",
        "foreign_keys": "ON",
        "busy_timeout": "5000",
        "synchronous": "NORMAL",
        "wal_autocheckpoint": "500",
    },
    # Class D: cache / fallback
    "D": {
        "journal_mode": "WAL",
        "foreign_keys": "ON",
        "busy_timeout": "3000",
        "synchronous": "NORMAL",
        "wal_autocheckpoint": "250",
    },
}


def apply_pragma(connection: sqlite3.Connection, *, db_class: str = "B") -> None:
    """Apply the central PRAGMA policy for the given database class.

    Args:
        connection: an open sqlite3.Connection
        db_class: one of 'A', 'B', 'C', 'D'
    """
    policy = _PRAGMA_POLICIES.get(db_class, _PRAGMA_POLICIES["B"])
    for pragma, value in policy.items():
        connection.execute(f"PRAGMA {pragma} = {value}")


def get_pragma_policy(db_class: str = "B") -> dict[str, str]:
    """Get the PRAGMA policy for a class without applying it."""
    return dict(_PRAGMA_POLICIES.get(db_class, _PRAGMA_POLICIES["B"]))


def get_all_policies() -> dict[str, dict[str, str]]:
    """Get all PRAGMA policies."""
    return {k: dict(v) for k, v in _PRAGMA_POLICIES.items()}


__all__ = ["apply_pragma", "get_pragma_policy", "get_all_policies"]
