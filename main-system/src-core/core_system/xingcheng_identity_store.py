"""Dedicated SQLite store for the Xingcheng personality identity.

A592/A604: the assistant's identity store moved to
``xingcheng_assistant_identity`` — each institution owns its own
database lifecycle; this module holds only the 星澄 personality store.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class XingchengIdentityStore:
    """Owns the 星澄 personality identity database only."""

    def __init__(self, project_root: Path) -> None:
        root = Path(project_root) / "main-system" / "runtime" / "data"
        self.personality_path = root / "xingcheng_identity.sqlite3"

    def initialize(self) -> None:
        self.personality_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.personality_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS personality_identity ("
                "identity_id TEXT PRIMARY KEY CHECK(identity_id = '星澄'), "
                "display_name TEXT NOT NULL CHECK(display_name = '星澄'), "
                "owner_kind TEXT NOT NULL CHECK(owner_kind = 'native-model'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO personality_identity "
                "(identity_id, display_name, owner_kind) VALUES (?, ?, ?)",
                ("星澄", "星澄", "native-model"),
            )

    def status(self) -> dict[str, Any]:
        return {
            "personality_database": self.personality_path.name,
            "shared_authority": False,
        }


__all__ = ["XingchengIdentityStore"]
