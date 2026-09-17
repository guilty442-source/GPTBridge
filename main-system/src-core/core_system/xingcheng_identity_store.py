"""Separate SQLite stores for Xingcheng and the Xingcheng Assistant."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class XingchengIdentityStores:
    """Own two non-interchangeable databases with distinct schemas."""

    def __init__(self, project_root: Path) -> None:
        root = project_root / "main-system" / "runtime" / "data"
        self.personality_path = root / "xingcheng_identity.sqlite3"
        self.assistant_path = root / "xingcheng_assistant_identity.sqlite3"

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
        with sqlite3.connect(self.assistant_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS assistant_identity_group ("
                "group_id TEXT PRIMARY KEY CHECK(group_id = 'xingcheng-assistant-identity-group'), "
                "display_name TEXT NOT NULL CHECK(display_name = '星澄助理'), "
                "entity_kind TEXT NOT NULL CHECK(entity_kind = 'independent-privileged-institution'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO assistant_identity_group "
                "(group_id, display_name, entity_kind) VALUES (?, ?, ?)",
                (
                    "xingcheng-assistant-identity-group",
                    "星澄助理",
                    "independent-privileged-institution",
                ),
            )

    def status(self) -> dict[str, Any]:
        return {
            "personality_database": self.personality_path.name,
            "assistant_database": self.assistant_path.name,
            "separate_files": self.personality_path != self.assistant_path,
            "shared_authority": False,
        }


__all__ = ["XingchengIdentityStores"]
