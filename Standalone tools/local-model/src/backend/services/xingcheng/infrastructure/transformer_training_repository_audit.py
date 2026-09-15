from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping

from .training_repo_schema import TransformerTrainingSchemaMixin


_AUDIT_INSERT_SQL = """
            INSERT INTO transformer_training_audit_event(
                event_id, event_type, entity_type, entity_id, payload_json,
                previous_event_sha256, event_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """


class TransformerTrainingAuditMixin(TransformerTrainingSchemaMixin):
    """Append-only, hash-chained audit events for training governance."""

    def _audit_event_body(
        self,
        *,
        event_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: Any,
        previous_sha256: str,
        created_at: str,
    ) -> str:
        return self._canonical_json(
            {
                "event_id": event_id,
                "event_type": str(event_type),
                "entity_type": str(entity_type),
                "entity_id": str(entity_id),
                "payload": payload,
                "previous_event_sha256": previous_sha256,
                "created_at": created_at,
            }
        )

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        event_id = f"star-transformer-audit-{uuid.uuid4().hex[:24]}"
        created_at = self._now()
        previous = connection.execute(
            """
            SELECT event_sha256 FROM transformer_training_audit_event
            ORDER BY sequence DESC LIMIT 1
            """
        ).fetchone()
        previous_sha256 = str(previous[0]) if previous else "0" * 64
        payload_json = self._canonical_json(dict(payload))
        event_body = self._audit_event_body(
            event_id=event_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=json.loads(payload_json),
            previous_sha256=previous_sha256,
            created_at=created_at,
        )
        event_sha256 = self._sha256_text(event_body)
        connection.execute(
            _AUDIT_INSERT_SQL,
            (
                event_id,
                str(event_type),
                str(entity_type),
                str(entity_id),
                payload_json,
                previous_sha256,
                event_sha256,
                created_at,
            ),
        )
        return {
            "event_id": event_id,
            "event_sha256": event_sha256,
            "previous_event_sha256": previous_sha256,
            "created_at": created_at,
        }

    def verify_audit_chain(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, entity_type, entity_id,
                       payload_json, previous_event_sha256, event_sha256,
                       created_at
                FROM transformer_training_audit_event
                ORDER BY sequence ASC
                """
            ).fetchall()
        expected_previous = "0" * 64
        for index, row in enumerate(rows, start=1):
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "event_count": len(rows),
                    "failed_sequence": index,
                    "reason": "invalid-payload-json",
                }
            event_body = self._audit_event_body(
                event_id=str(row["event_id"]),
                event_type=str(row["event_type"]),
                entity_type=str(row["entity_type"]),
                entity_id=str(row["entity_id"]),
                payload=payload,
                previous_sha256=str(row["previous_event_sha256"]),
                created_at=str(row["created_at"]),
            )
            if (
                str(row["previous_event_sha256"]) != expected_previous
                or self._sha256_text(event_body) != str(row["event_sha256"])
            ):
                return {
                    "ok": False,
                    "event_count": len(rows),
                    "failed_sequence": index,
                    "reason": "audit-chain-mismatch",
                }
            expected_previous = str(row["event_sha256"])
        return {
            "ok": True,
            "event_count": len(rows),
            "head_sha256": expected_previous,
        }
