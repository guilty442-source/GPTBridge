from __future__ import annotations

import json
import sqlite3
from typing import Any

from ._helpers import _utc_now


class EntityHistoryMixin:
    @staticmethod
    def _record_row_history(
        connection: sqlite3.Connection,
        entity_type: str,
        entity_key: str,
        action: str,
        row: sqlite3.Row | dict[str, Any] | None,
    ) -> None:
        """Append an immutable snapshot inside the caller's transaction."""
        if row is None:
            return
        snapshot = dict(row)
        version_row = connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM vaultly_entity_history
            WHERE entity_type = ? AND entity_key = ?
            """,
            (entity_type, entity_key),
        ).fetchone()
        version = int(version_row["next_version"] if version_row is not None else 1)
        connection.execute(
            """
            INSERT INTO vaultly_entity_history (
                entity_type, entity_key, version, action, snapshot_json, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_key,
                version,
                action,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                _utc_now(),
            ),
        )

    @staticmethod
    def _row_by_key(
        connection: sqlite3.Connection,
        table: str,
        key_column: str,
        key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT * FROM {table} WHERE {key_column} = ?",
            (key,),
        ).fetchone()

    def list_entity_history(
        self,
        entity_type: str = "",
        entity_key: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if entity_type:
            filters.append("entity_type = ?")
            params.append(str(entity_type))
        if entity_key:
            filters.append("entity_key = ?")
            params.append(str(entity_key))
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT history_id, entity_type, entity_key, version, action,
                       snapshot_json, recorded_at
                FROM vaultly_entity_history
                {where_sql}
                ORDER BY history_id DESC
                LIMIT ?
                """,
                (*params, max(1, min(5000, int(limit)))),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["snapshot"] = json.loads(str(item.pop("snapshot_json")))
            except json.JSONDecodeError:
                item["snapshot"] = {}
            output.append(item)
        return output

    def restore_entity_history(self, history_id: int) -> dict[str, Any] | None:
        """Restore one audited snapshot without deleting the newer audit trail."""
        entity_tables = {
            "account": ("vaultly_accounts", "account_id"),
            "post": ("vaultly_posts", "post_id"),
            "post_media": ("vaultly_post_media", "media_id"),
            "media_history": ("vaultly_media_history", "dedupe_key"),
            "setting": ("vaultly_settings", "key"),
            "filter_term": ("vaultly_filter_terms", "term"),
            "retained_account": ("vaultly_retained_accounts", "account_id"),
            "removed_account": ("vaultly_removed_accounts", "account_id"),
        }
        with self._connect() as connection:
            history = connection.execute(
                """
                SELECT entity_type, entity_key, snapshot_json
                FROM vaultly_entity_history
                WHERE history_id = ?
                """,
                (int(history_id),),
            ).fetchone()
            if history is None:
                return None
            entity_type = str(history["entity_type"])
            mapping = entity_tables.get(entity_type)
            if mapping is None:
                return None
            try:
                snapshot = json.loads(str(history["snapshot_json"]))
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(snapshot, dict):
                return None

            table, key_column = mapping
            entity_key = str(history["entity_key"])
            current = self._row_by_key(connection, table, key_column, entity_key)
            self._record_row_history(
                connection,
                entity_type,
                entity_key,
                "superseded_by_restore",
                current,
            )

            allowed_columns = [
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                if str(row["name"]) in snapshot
            ]
            if key_column not in allowed_columns:
                return None
            values = [snapshot[column] for column in allowed_columns]
            assignments = ", ".join(
                f"{column} = excluded.{column}"
                for column in allowed_columns
                if column != key_column
            )
            connection.execute(
                f"""
                INSERT INTO {table} ({", ".join(allowed_columns)})
                VALUES ({", ".join("?" for _ in allowed_columns)})
                ON CONFLICT({key_column}) DO UPDATE SET {assignments}
                """,
                values,
            )
            restored = self._row_by_key(connection, table, key_column, entity_key)
            self._record_row_history(
                connection,
                entity_type,
                entity_key,
                "restored_version",
                restored,
            )
            return dict(restored) if restored is not None else None
