from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LocalCommandParser:
    """Parse zh-TW commands and own their local command/tag records."""

    DEFAULT_COMMANDS = (
        ("summarize", "摘要內容", "請用繁體中文整理重點摘要。", "conversation"),
        ("explain", "解釋內容", "請用繁體中文清楚解釋這個主題。", "conversation"),
        ("compare", "比較差異", "請比較各方案的差異、優缺點與適用情境。", "reasoning"),
        ("review-code", "檢查程式碼", "請檢查程式碼問題並提出修正建議。", "coding"),
        ("find-cause", "分析原因", "請分析問題根因並列出影響範圍。", "reasoning"),
        ("optimize", "優化方案", "請在兼顧效能與系統負載下提出優化方案。", "reasoning"),
        ("plan", "建立步驟", "請依優先順序建立可執行步驟。", "conversation"),
        ("verify", "核對結果", "請核對結果的正確性與遺漏項目。", "reasoning"),
    )
    DEFAULT_TAGS = (
        ("conversation", "對話", "一般問答與內容整理", "blue"),
        ("reasoning", "推理", "分析、比較與決策", "violet"),
        ("coding", "程式", "程式碼檢查與開發", "emerald"),
        ("frequent", "常用", "常用指令", "amber"),
        ("default", "預設", "系統預設指令", "slate"),
    )

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    @staticmethod
    def normalize(command_text: str) -> str:
        return " ".join(str(command_text or "").strip().split())[:4_000]

    @staticmethod
    def infer_intent(command_text: str, fallback: str = "conversation") -> str:
        text = str(command_text or "").casefold()
        rules = (
            ("coding", ("程式", "程式碼", "函式", "類別", "修正", "重構", "debug", "code")),
            ("reasoning", ("分析", "原因", "比較", "推理", "評估", "驗證", "為什麼")),
            ("search", ("搜尋", "查詢", "尋找", "最新", "資料")),
            ("calculation", ("計算", "統計", "公式", "比例", "百分比")),
            ("reading", ("摘要", "整理", "閱讀", "文件", "重點")),
        )
        return next((intent for intent, words in rules if any(word in text for word in words)), fallback)

    def parse(self, command_text: str, semantic_plan: dict[str, Any]) -> dict[str, Any]:
        plan = dict(semantic_plan)
        normalized = self.normalize(command_text)
        current = str(plan.get("primary_intent") or "conversation")
        local_intent = self.infer_intent(normalized, current)
        intents = [str(item) for item in plan.get("intents") or [] if str(item)]
        if local_intent != "conversation" and current == "conversation":
            plan["primary_intent"] = local_intent
            plan["intents"] = [
                local_intent, *[item for item in intents if item != local_intent]
            ]
        plan["local_command_parser"] = {
            "language": "zh-TW",
            "normalized_command": normalized,
            "matched_intent": local_intent,
            "database_managed": True,
            "sql_auto_adapt": True,
        }
        return plan

    def initialize(self, connection: sqlite3.Connection) -> None:
        self._adapt_schema(connection)
        now = datetime.now(timezone.utc).isoformat()
        for tag_id, label, description, color in self.DEFAULT_TAGS:
            connection.execute(
                "INSERT INTO command_tag (tag_id,label,description,color,is_default,enabled,created_at) "
                "VALUES (?,?,?,?,1,1,?) ON CONFLICT(tag_id) DO UPDATE SET "
                "label=excluded.label,description=excluded.description,color=excluded.color,enabled=1",
                (tag_id, label, description, color, now),
            )
        for command_id, title, command_text, intent in self.DEFAULT_COMMANDS:
            digest = hashlib.sha256(command_text.encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO common_command (command_id,command_hash,title,command_text,intent,usage_count,is_default,enabled,created_at,last_used_at) "
                "VALUES (?,?,?,?,?,0,1,1,?,?) ON CONFLICT(command_id) DO UPDATE SET "
                "command_hash=excluded.command_hash,title=excluded.title,command_text=excluded.command_text,intent=excluded.intent,is_default=1,enabled=1",
                (command_id, digest, title, command_text, intent, now, now),
            )
            for tag_id in (intent, "default"):
                connection.execute(
                    "INSERT OR IGNORE INTO common_command_tag (command_id,tag_id,created_at) VALUES (?,?,?)",
                    (command_id, tag_id, now),
                )

    @staticmethod
    def _adapt_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS common_command (
                command_id TEXT PRIMARY KEY,
                command_hash TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                command_text TEXT NOT NULL,
                intent TEXT NOT NULL DEFAULT 'conversation',
                usage_count INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT '',
                last_used_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS command_tag (
                tag_id TEXT PRIMARY KEY,
                label TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT 'slate',
                is_default INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS common_command_tag (
                command_id TEXT NOT NULL,
                tag_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(command_id, tag_id),
                FOREIGN KEY(command_id) REFERENCES common_command(command_id),
                FOREIGN KEY(tag_id) REFERENCES command_tag(tag_id)
            );
            CREATE INDEX IF NOT EXISTS idx_common_command_usage
                ON common_command(enabled, usage_count DESC, last_used_at DESC);
            CREATE INDEX IF NOT EXISTS idx_common_command_tag_tag
                ON common_command_tag(tag_id, command_id);
            """
        )
        required_columns = {
            "common_command": {
                "title": "TEXT NOT NULL DEFAULT ''",
                "intent": "TEXT NOT NULL DEFAULT 'conversation'",
                "usage_count": "INTEGER NOT NULL DEFAULT 0",
                "is_default": "INTEGER NOT NULL DEFAULT 0",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "last_used_at": "TEXT NOT NULL DEFAULT ''",
            },
            "command_tag": {
                "description": "TEXT NOT NULL DEFAULT ''",
                "color": "TEXT NOT NULL DEFAULT 'slate'",
                "is_default": "INTEGER NOT NULL DEFAULT 0",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in required_columns.items():
            existing = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for column, declaration in columns.items():
                if column not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def record(self, connection: sqlite3.Connection, command_text: str, intent: str = "") -> dict[str, Any]:
        normalized = self.normalize(command_text)
        if not normalized:
            return {"recorded": False}
        resolved_intent = self.infer_intent(normalized, intent or "conversation")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        command_id = f"user-{digest[:24]}"
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "INSERT OR IGNORE INTO command_tag (tag_id,label,description,color,is_default,enabled,created_at) "
            "VALUES (?,?,'自動依指令意圖建立','slate',0,1,?)",
            (resolved_intent, resolved_intent, now),
        )
        connection.execute(
            "INSERT INTO common_command (command_id,command_hash,title,command_text,intent,usage_count,is_default,enabled,created_at,last_used_at) "
            "VALUES (?,?, '',?,?,1,0,1,?,?) ON CONFLICT(command_hash) DO UPDATE SET "
            "usage_count=common_command.usage_count+1,intent=excluded.intent,last_used_at=excluded.last_used_at",
            (command_id, digest, normalized, resolved_intent, now, now),
        )
        row = connection.execute(
            "SELECT command_id,usage_count FROM common_command WHERE command_hash=?", (digest,)
        ).fetchone()
        stored_id, usage_count = str(row[0]), int(row[1])
        for tag_id in (resolved_intent, "frequent" if usage_count >= 3 else ""):
            if tag_id:
                connection.execute(
                    "INSERT OR IGNORE INTO common_command_tag (command_id,tag_id,created_at) VALUES (?,?,?)",
                    (stored_id, tag_id, now),
                )
        return {"recorded": True, "command_id": stored_id, "intent": resolved_intent, "usage_count": usage_count}

    def list(self, connection: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT command_id,title,command_text,intent,usage_count,is_default,last_used_at,"
            "COALESCE((SELECT json_group_array(t.label) FROM common_command_tag ct "
            "JOIN command_tag t ON t.tag_id=ct.tag_id WHERE ct.command_id=common_command.command_id AND t.enabled=1),'[]') tags_json "
            "FROM common_command WHERE enabled=1 ORDER BY usage_count DESC,is_default DESC,last_used_at DESC LIMIT ?",
            (max(1, min(100, int(limit))),),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["tags"] = json.loads(str(item.pop("tags_json") or "[]"))
            result.append(item)
        return result


__all__ = ["LocalCommandParser"]
