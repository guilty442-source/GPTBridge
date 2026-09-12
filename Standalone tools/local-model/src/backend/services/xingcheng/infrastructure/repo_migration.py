from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


class MigrationMixin:
    """Database schema migration and compaction logic for LocalAiRepository."""

    def _migrate_and_compact(self, connection: sqlite3.Connection) -> dict[str, int]:
        before_distribution = int(
            connection.execute("SELECT COUNT(*) FROM distribution_event").fetchone()[0]
        )
        before_search = int(
            connection.execute("SELECT COUNT(*) FROM web_search_log").fetchone()[0]
        )
        if "event_fingerprint" not in self._columns(connection, "distribution_event"):
            connection.execute(
                "ALTER TABLE distribution_event ADD COLUMN event_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        search_columns = self._columns(connection, "web_search_log")
        if "request_hash" not in search_columns:
            connection.execute(
                "ALTER TABLE web_search_log ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''"
            )
        if "occurrence_count" not in search_columns:
            connection.execute(
                "ALTER TABLE web_search_log ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1"
            )
        if "last_seen_at" not in search_columns:
            connection.execute(
                "ALTER TABLE web_search_log ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''"
            )
        memory_columns = self._columns(connection, "model_memory")
        memory_migrations = {
            "review_status": "TEXT NOT NULL DEFAULT 'approved'",
            "reviewed_by": "TEXT NOT NULL DEFAULT ''",
            "reviewed_at": "TEXT NOT NULL DEFAULT ''",
            "review_reason": "TEXT NOT NULL DEFAULT ''",
            "revoked_at": "TEXT NOT NULL DEFAULT ''",
            "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, declaration in memory_migrations.items():
            if column not in memory_columns:
                connection.execute(
                    f"ALTER TABLE model_memory ADD COLUMN {column} {declaration}"
                )

        connection.execute(
            """
            DELETE FROM distribution_event
            WHERE trim(ex_date) = '' AND trim(record_date) = ''
              AND trim(payment_date) = '' AND amount_per_unit IS NULL
              AND trim(title) = ''
            """
        )
        rows = connection.execute(
            """
            SELECT id, identity_key, ex_date, record_date, payment_date,
                   amount_per_unit, currency, frequency, title, source_url, observed_at
            FROM distribution_event
            """
        ).fetchall()
        for row in rows:
            event = {
                "ex_date": row[2],
                "record_date": row[3],
                "payment_date": row[4],
                "amount_per_unit": row[5],
                "currency": row[6],
                "frequency": row[7],
                "title": row[8],
                "source_url": row[9],
                "observed_at": row[10],
            }
            connection.execute(
                "UPDATE distribution_event SET event_fingerprint = ? WHERE id = ?",
                (self._event_fingerprint(str(row[1]), event), int(row[0])),
            )
        connection.execute(
            """
            DELETE FROM distribution_event
            WHERE event_fingerprint <> ''
              AND id NOT IN (
                  SELECT MIN(id) FROM distribution_event
                  WHERE event_fingerprint <> '' GROUP BY event_fingerprint
              )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_distribution_event_fingerprint ON distribution_event(event_fingerprint) WHERE event_fingerprint <> ''"
        )

        search_rows = connection.execute(
            """
            SELECT id, query_type, request_json, response_json, created_at,
                   occurrence_count, last_seen_at
            FROM web_search_log ORDER BY id
            """
        ).fetchall()
        grouped: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        for row in search_rows:
            request_hash = self._request_hash(str(row[2]))
            grouped.setdefault((str(row[1]), request_hash), []).append(row)
        for (_query_type, request_hash), duplicates in grouped.items():
            keeper = duplicates[-1]
            try:
                raw_response = json.loads(str(keeper[3]))
            except json.JSONDecodeError:
                raw_response = {}
            summary = self._search_response_summary(
                raw_response if isinstance(raw_response, dict) else {}
            )
            occurrence_count = sum(max(1, int(row[5] or 1)) for row in duplicates)
            created_at = min(str(row[4] or "") for row in duplicates)
            last_seen_at = max(str(row[6] or row[4] or "") for row in duplicates)
            keeper_id = int(keeper[0])
            connection.execute(
                """
                UPDATE web_search_log
                SET response_json = ?, request_hash = ?, occurrence_count = ?,
                    created_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                    request_hash,
                    occurrence_count,
                    created_at,
                    last_seen_at,
                    keeper_id,
                ),
            )
            if len(duplicates) > 1:
                connection.executemany(
                    "DELETE FROM web_search_log WHERE id = ?",
                    [(int(row[0]),) for row in duplicates[:-1]],
                )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_search_request_hash ON web_search_log(query_type, request_hash) WHERE request_hash <> ''"
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.SEARCH_LOG_RETENTION_DAYS)).isoformat()
        connection.execute(
            "DELETE FROM web_search_log WHERE COALESCE(NULLIF(last_seen_at, ''), created_at) < ?",
            (cutoff,),
        )
        connection.execute(
            """
            DELETE FROM web_search_log WHERE id NOT IN (
                SELECT id FROM web_search_log
                ORDER BY COALESCE(NULLIF(last_seen_at, ''), created_at) DESC
                LIMIT ?
            )
            """,
            (self.MAX_SEARCH_LOGS,),
        )
        after_distribution = int(
            connection.execute("SELECT COUNT(*) FROM distribution_event").fetchone()[0]
        )
        after_search = int(
            connection.execute("SELECT COUNT(*) FROM web_search_log").fetchone()[0]
        )
        return {
            "distribution_events_removed": before_distribution - after_distribution,
            "search_logs_removed": before_search - after_search,
        }
