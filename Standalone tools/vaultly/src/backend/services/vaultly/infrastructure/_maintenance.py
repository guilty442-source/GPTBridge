from __future__ import annotations

from typing import Any

_FINISHED_STATUSES = ("completed", "failed", "cancelled")

# Bounded history policy for tool-internal tables. Dedupe records
# (vaultly_posts / vaultly_post_media / vaultly_media_history) are never
# pruned — deleting them would re-download media. removed_accounts is the
# user-facing restore list and is likewise kept. Media files under the
# user-selected destination are outside tool governance entirely.
_KEEP_FINISHED_JOBS = 200
_KEEP_FINISHED_POST_SCAN_JOBS = 100
_KEEP_ENTITY_HISTORY_VERSIONS = 20


class MaintenanceMixin:
    def prune_state(self) -> dict[str, Any]:
        """Bound history-table growth; returns per-table deletion counts."""
        deleted: dict[str, int] = {
            "vaultly_jobs": 0,
            "vaultly_post_scan_jobs": 0,
            "vaultly_entity_history": 0,
        }
        with self._connect() as connection:
            deleted["vaultly_jobs"] = self._prune_finished(
                connection,
                "vaultly_jobs",
                "job_id",
                _KEEP_FINISHED_JOBS,
            )
            deleted["vaultly_post_scan_jobs"] = self._prune_finished(
                connection,
                "vaultly_post_scan_jobs",
                "scan_job_id",
                _KEEP_FINISHED_POST_SCAN_JOBS,
            )
            deleted["vaultly_entity_history"] = self._prune_entity_history(
                connection,
                _KEEP_ENTITY_HISTORY_VERSIONS,
            )
        return {"ok": True, "deleted": deleted}

    @staticmethod
    def _prune_finished(
        connection: Any,
        table: str,
        key_column: str,
        keep: int,
    ) -> int:
        placeholders = ", ".join("?" for _ in _FINISHED_STATUSES)
        cursor = connection.execute(  # sql-ok: table/column are fixed literals
            f"""
            DELETE FROM {table}
            WHERE {key_column} IN (
                SELECT {key_column} FROM {table}
                WHERE status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (*_FINISHED_STATUSES, int(keep)),
        )
        return int(cursor.rowcount if cursor.rowcount is not None else 0)

    @staticmethod
    def _prune_entity_history(connection: Any, keep_per_entity: int) -> int:
        cursor = connection.execute(
            """
            DELETE FROM vaultly_entity_history
            WHERE history_id IN (
                SELECT history_id FROM (
                    SELECT history_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY entity_type, entity_key
                               ORDER BY version DESC
                           ) AS version_rank
                    FROM vaultly_entity_history
                )
                WHERE version_rank > ?
            )
            """,
            (int(keep_per_entity),),
        )
        return int(cursor.rowcount if cursor.rowcount is not None else 0)
