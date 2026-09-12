from __future__ import annotations

from typing import Any


class StatusMixin:
    """Database status reporting and repair for LocalAiRepository."""

    def database_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "inference_log",
                    "instrument_identity",
                    "market_observation",
                    "distribution_event",
                    "investment_parameter_definition",
                    "investment_model_definition",
                    "investment_parameter_adjustment",
                    "mathematical_capability_definition",
                    "web_search_log",
                    "model_memory",
                    "language_training_example",
                    "language_model_maintenance",
                    "code_upgrade_proposal",
                    "capability_composition",
                )
            }
            blank_distribution_events = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM distribution_event
                    WHERE trim(ex_date) = '' AND trim(record_date) = ''
                      AND trim(payment_date) = '' AND amount_per_unit IS NULL
                      AND trim(title) = ''
                    """
                ).fetchone()[0]
            )
            duplicate_distribution_events = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(total - 1), 0) FROM (
                        SELECT COUNT(*) AS total FROM distribution_event
                        WHERE event_fingerprint <> '' GROUP BY event_fingerprint
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
            )
            max_search_response_chars = int(
                connection.execute(
                    "SELECT COALESCE(MAX(length(response_json)), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            search_payload_chars = int(
                connection.execute(
                    "SELECT COALESCE(SUM(length(request_json) + length(response_json)), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            search_occurrences = int(
                connection.execute(
                    "SELECT COALESCE(SUM(occurrence_count), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            search_success_count = int(
                connection.execute(
                    "SELECT COALESCE(SUM(CASE WHEN ok = 1 THEN occurrence_count ELSE 0 END), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            search_failure_count = int(
                connection.execute(
                    "SELECT COALESCE(SUM(CASE WHEN ok = 0 THEN occurrence_count ELSE 0 END), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            memory_review_counts = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT review_status, COUNT(*) FROM model_memory GROUP BY review_status"
                ).fetchall()
            }
            latest_market_observation_at = str(
                connection.execute(
                    "SELECT COALESCE(MAX(observed_at), '') FROM market_observation"
                ).fetchone()[0]
                or ""
            )
            latest_inference_at = str(
                connection.execute(
                    "SELECT COALESCE(MAX(created_at), '') FROM inference_log"
                ).fetchone()[0]
                or ""
            )
            language_training_quality = connection.execute(
                """
                SELECT COUNT(*), COALESCE(AVG(quality_score), 0),
                       COALESCE(MAX(revision), 0), COALESCE(MAX(created_at), '')
                FROM language_training_example WHERE active = 1
                """
            ).fetchone()
            latest_language_maintenance = connection.execute(
                """
                SELECT run_id, action, ok, created_at
                FROM language_model_maintenance ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        return {
            "engine": "local-sqlite3-degraded",
            "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
            "canonical_central_engine": "postgresql",
            "canonical": False,
            "authority": "non-canonical-reconciliation-required",
            "reconciliation_required": True,
            "path": str(self.database_path),
            "database_scope": self.database_scope,
            "owner_model_id": self.owner_model_id,
            "isolation_enforced": True,
            "tables": counts,
            "size_bytes": page_count * page_size,
            "quality": {
                "blank_distribution_events": blank_distribution_events,
                "duplicate_distribution_events": duplicate_distribution_events,
                "search_request_occurrences": search_occurrences,
                "search_success_count": search_success_count,
                "search_failure_count": search_failure_count,
                "search_success_rate_percent": round(
                    search_success_count / search_occurrences * 100, 4
                )
                if search_occurrences
                else None,
                "search_payload_chars": search_payload_chars,
                "max_search_response_chars": max_search_response_chars,
                "search_log_retention_days": self.SEARCH_LOG_RETENTION_DAYS,
                "search_log_limit": self.MAX_SEARCH_LOGS,
                "memory_review_counts": memory_review_counts,
                "latest_market_observation_at": latest_market_observation_at,
                "latest_inference_at": latest_inference_at,
                "language_training": {
                    "active_example_count": int(language_training_quality[0]),
                    "average_quality_score": round(float(language_training_quality[1]), 4),
                    "latest_revision": int(language_training_quality[2]),
                    "latest_trained_at": str(language_training_quality[3]),
                    "maximum_examples": self.MAX_LANGUAGE_TRAINING_EXAMPLES,
                },
                "latest_language_maintenance": {
                    "run_id": str(latest_language_maintenance[0]),
                    "action": str(latest_language_maintenance[1]),
                    "ok": bool(latest_language_maintenance[2]),
                    "created_at": str(latest_language_maintenance[3]),
                }
                if latest_language_maintenance is not None
                else None,
                "sqlite_integrity": integrity_check,
            },
        }

    def repair_data(self, *, vacuum: bool = True) -> dict[str, Any]:
        with self._connect() as connection:
            changes = self._migrate_and_compact(connection)
        vacuumed = False
        if vacuum:
            with self._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
                vacuumed = True
        return {**changes, "vacuumed": vacuumed, "database": self.database_status()}
