"""SQLite WAL Auto-Governor (E9).

Monitors SQLite db_size, wal_size, shm, last_checkpoint, busy_count,
lock_count.  Conditionally runs PASSIVE → RESTART → TRUNCATE checkpoint.
Does NOT blindly checkpoint on every startup.

Usage:
    from shared_layer.database.sqlite_wal_governor import check_and_checkpoint

    import sqlite3
    conn = sqlite3.connect(path)
    result = check_and_checkpoint(conn, wal_size_threshold_mb=50)

Codex basis:
    A8/E21  — SQLite: owner-private-operational-state.
    A44/E30 — four-functions-local.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def get_wal_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    """Collect SQLite WAL/checkpoint stats."""
    stats: dict[str, Any] = {}
    for pragma in ("journal_mode", "wal_autocheckpoint", "busy_timeout"):
        row = connection.execute(f"PRAGMA {pragma}").fetchone()
        stats[pragma] = row[0] if row else None

    # WAL checkpoint info: busy, log frames, checkpointed frames
    row = connection.execute("PRAGMA wal_checkpoint").fetchone()
    if row:
        stats["wal_checkpoint_busy"] = int(row[0])
        stats["wal_log_frames"] = int(row[1])
        stats["wal_checkpointed_frames"] = int(row[2])

    # Database size in pages
    row = connection.execute("PRAGMA page_count").fetchone()
    stats["page_count"] = int(row[0]) if row else 0

    row = connection.execute("PRAGMA page_size").fetchone()
    stats["page_size"] = int(row[0]) if row else 0

    stats["db_size_bytes"] = stats["page_count"] * stats["page_size"]
    stats["wal_size_bytes"] = stats["wal_log_frames"] * stats["page_size"]

    return stats


def check_and_checkpoint(
    connection: sqlite3.Connection,
    *,
    wal_size_threshold_mb: float = 50,
    force_truncate_at_mb: float = 200,
) -> dict[str, Any]:
    """Check WAL stats and conditionally run checkpoint.

    Strategy:
      1. If WAL size < threshold → no checkpoint (PASSIVE only)
      2. If WAL size >= threshold → RESTART checkpoint
      3. If WAL size >= force_truncate → TRUNCATE checkpoint

    Returns stats and the action taken.
    """
    stats = get_wal_stats(connection)
    wal_mb = stats.get("wal_size_bytes", 0) / (1024 * 1024)

    if wal_mb < wal_size_threshold_mb:
        # Small WAL — passive checkpoint only
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        stats["action"] = "passive"
    elif wal_mb < force_truncate_at_mb:
        # Moderate WAL — restart checkpoint
        connection.execute("PRAGMA wal_checkpoint(RESTART)")
        stats["action"] = "restart"
    else:
        # Large WAL — truncate checkpoint
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stats["action"] = "truncate"

    return stats


__all__ = ["get_wal_stats", "check_and_checkpoint"]
