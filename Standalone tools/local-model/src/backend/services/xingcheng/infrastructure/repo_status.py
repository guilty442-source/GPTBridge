from __future__ import annotations

from typing import Any


class StatusMixin:
    """Database status reporting and repair for LocalAiRepository."""

    def database_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # sql-ok: fixed/introspected identifiers
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
            integrity_check = "ok"
            size_row = connection.execute(
                "SELECT COALESCE(SUM(pg_total_relation_size("
                "quote_ident(table_schema) || '.' || quote_ident(table_name))), 0) "
                "FROM information_schema.tables WHERE table_schema = current_schema"
            ).fetchone()
            size_bytes = int(size_row[0]) if size_row else 0
        return {
            "engine": "postgresql",
            "canonical_central_engine": "postgresql",
            "canonical": False,
            "authority": "module-private-postgresql",
            "reconciliation_required": False,
            "path": str(self.database_path),
            "database_scope": self.database_scope,
            "owner_model_id": self.owner_model_id,
            "isolation_enforced": True,
            "tables": counts,
            "size_bytes": size_bytes,
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
                "language_training": {"retired": True, "authority": "B167/B38"},
                "engine_integrity": integrity_check,
            },
        }

    def repair_data(self, *, vacuum: bool = True) -> dict[str, Any]:
        with self._connect() as connection:
            changes = self._migrate_and_compact(connection)
        vacuumed = False
        if vacuum:
            from shared_layer.local.pg_adapter import connect as pg_connect

            with pg_connect(
                f"gptbridge_xingcheng_{self.database_scope}", autocommit=True
            ) as connection:
                connection.execute("VACUUM")
                vacuumed = True
        return {**changes, "vacuumed": vacuumed, "database": self.database_status()}
