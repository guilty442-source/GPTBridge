"""xingcheng_assistant_identity — 星澄助理獨立身分組.

星澄助理（Xingcheng Assistant）是與主宰同級的獨立特權機構
（A144/A145/A156）：負責獨立稽查、使用者控制與結果呈現，不編程、
不產生修補、不執行修復，也不隸屬星澄原生模型。

A592/A604 剝離：助理身分組與其專用資料庫由此模組自行持有，不再經由
``xingcheng_personality`` / ``xingcheng_identity_store`` 初始化——兩個機構
各用專用資料庫，禁止混用或互相作為權威來源（architecture-project §身分組）。

身分常量來源：Governance Codex（星澄助理的機構身份與權力邊界）。
此模組為唯讀協調層，不執行任何 AI/模型推理。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


# 機構身份常量 — 來自 Governance Codex
XINGCHENG_ASSISTANT_IDENTITY_GROUP = "xingcheng-assistant-identity-group"
XINGCHENG_ASSISTANT_DISPLAY_NAME = "星澄助理"
XINGCHENG_ASSISTANT_ENTITY_KIND = "independent-privileged-institution"
ASSISTANT_IDENTITY_DATABASE = "xingcheng_assistant_identity"


class XingchengAssistantIdentityStore:
    """Owns the assistant's dedicated identity database — nothing else."""

    def __init__(self, project_root: Path) -> None:
        root = Path(project_root) / "main-system" / "runtime" / "data"
        self.assistant_path = root / f"{ASSISTANT_IDENTITY_DATABASE}.sqlite3"

    def initialize(self) -> None:
        self.assistant_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.assistant_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS assistant_identity_group ("
                "group_id TEXT PRIMARY KEY CHECK(group_id = "
                "'xingcheng-assistant-identity-group'), "
                "display_name TEXT NOT NULL CHECK(display_name = '星澄助理'), "
                "entity_kind TEXT NOT NULL CHECK(entity_kind = "
                "'independent-privileged-institution'))"
            )
            connection.execute(
                "INSERT OR IGNORE INTO assistant_identity_group "
                "(group_id, display_name, entity_kind) VALUES (?, ?, ?)",
                (
                    XINGCHENG_ASSISTANT_IDENTITY_GROUP,
                    XINGCHENG_ASSISTANT_DISPLAY_NAME,
                    XINGCHENG_ASSISTANT_ENTITY_KIND,
                ),
            )

    def status(self) -> dict[str, Any]:
        return {
            "assistant_database": self.assistant_path.name,
            "shared_authority": False,
        }


def assistant_identity_status(project_root: Path) -> dict[str, Any]:
    """星澄助理身分組的唯讀快照（不含星澄人格欄位）。"""
    return {
        "assistant_identity": XINGCHENG_ASSISTANT_IDENTITY_GROUP,
        "assistant_display_name": XINGCHENG_ASSISTANT_DISPLAY_NAME,
        "assistant_entity_kind": XINGCHENG_ASSISTANT_ENTITY_KIND,
        "assistant_identity_database": ASSISTANT_IDENTITY_DATABASE,
        "identity_stores": XingchengAssistantIdentityStore(
            project_root
        ).status(),
    }


__all__ = [
    "ASSISTANT_IDENTITY_DATABASE",
    "XINGCHENG_ASSISTANT_DISPLAY_NAME",
    "XINGCHENG_ASSISTANT_ENTITY_KIND",
    "XINGCHENG_ASSISTANT_IDENTITY_GROUP",
    "XingchengAssistantIdentityStore",
    "assistant_identity_status",
]
