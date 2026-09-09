from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .generative_language_model import MIN_TRAINING_GROUNDING_COVERAGE


class LanguageTrainingMixin:
    """Inference recording, language training, and model maintenance for LocalAiRepository."""

    def record(self, model: str, request: dict[str, Any], response: dict[str, Any]) -> None:
        if str(model).strip() != self.owner_model_id:
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO inference_log(model, request_json, response_json) VALUES (?, ?, ?)",
                (
                    model,
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(response, ensure_ascii=False),
                ),
            )

    def store_language_training_example(
        self,
        *,
        intent: str,
        input_text: str,
        target_text: str,
        source_type: str,
        quality_score: float,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_intent = str(intent or "capabilities").strip().casefold()[:64]
        normalized_input = str(input_text or "").strip()[:16_000]
        normalized_target = str(target_text or "").strip()[:16_000]
        normalized_source = str(source_type or "self-distillation-grounded").strip()[:96]
        quality = max(0.0, min(1.0, float(quality_score)))
        if not normalized_input or not normalized_target:
            raise ValueError("language training input and target are required")
        if quality < 0.8:
            raise ValueError("language training quality gate rejected the example")
        content_hash = hashlib.sha256(
            f"{normalized_intent}\0{normalized_input}\0{normalized_target}".encode("utf-8")
        ).hexdigest()
        example_id = f"star-train-{content_hash[:24]}"
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO language_training_example(
                    example_id, content_hash, intent, input_text, target_text,
                    source_type, quality_score, validation_json, active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    example_id,
                    content_hash,
                    normalized_intent,
                    normalized_input,
                    normalized_target,
                    normalized_source,
                    quality,
                    json.dumps(validation, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            inserted = cursor.rowcount > 0
            connection.execute(
                """
                UPDATE language_training_example SET active = 0
                WHERE revision NOT IN (
                    SELECT revision FROM language_training_example
                    WHERE active = 1 ORDER BY revision DESC LIMIT ?
                )
                """,
                (self.MAX_LANGUAGE_TRAINING_EXAMPLES,),
            )
            row = connection.execute(
                """
                SELECT revision, example_id, content_hash, intent, input_text,
                       target_text, source_type, quality_score, validation_json,
                       active, created_at
                FROM language_training_example WHERE content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        if row is None:
            raise RuntimeError("language training example was not stored")
        return {
            "revision": int(row[0]),
            "example_id": str(row[1]),
            "content_hash": str(row[2]),
            "intent": str(row[3]),
            "input_text": str(row[4]),
            "target_text": str(row[5]),
            "source_type": str(row[6]),
            "quality_score": float(row[7]),
            "validation": json.loads(str(row[8]) or "{}"),
            "active": bool(row[9]),
            "created_at": str(row[10]),
            "inserted": inserted,
            "owner_model_id": self.owner_model_id,
        }

    def language_training_examples(self, *, limit: int = 500) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(self.MAX_LANGUAGE_TRAINING_EXAMPLES, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision, example_id, intent, input_text, target_text,
                       source_type, quality_score, validation_json, created_at
                FROM language_training_example
                WHERE active = 1 AND quality_score >= 0.8
                ORDER BY revision ASC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [
            {
                "revision": int(row[0]),
                "example_id": str(row[1]),
                "intent": str(row[2]),
                "input_text": str(row[3]),
                "target_text": str(row[4]),
                "source_type": str(row[5]),
                "quality_score": float(row[6]),
                "validation": json.loads(str(row[7]) or "{}"),
                "created_at": str(row[8]),
            }
            for row in rows
        ]

    def native_private_context(self, *, limit: int = 6) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        bounded_limit = max(1, min(12, int(limit)))
        with self._connect() as connection:
            training_rows = connection.execute(
                """
                SELECT example_id, intent, input_text, target_text, source_type,
                       quality_score, created_at
                FROM language_training_example
                WHERE active = 1 AND quality_score >= 0.8
                ORDER BY revision DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
            capability_rows = connection.execute(
                """
                SELECT composition_id, status, implementation_target, updated_at
                FROM capability_composition
                ORDER BY updated_at DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
            operation_rows = connection.execute(
                """
                SELECT id, model, request_json, response_json, created_at
                FROM inference_log
                ORDER BY id DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return {
            "owner_model_id": self.owner_model_id,
            "database_scope": self.database_scope,
            "training_examples": [
                {
                    "example_id": str(row[0]),
                    "intent": str(row[1]),
                    "input_text": str(row[2])[:2_000],
                    "target_text": str(row[3])[:2_000],
                    "source_type": str(row[4]),
                    "quality_score": float(row[5]),
                    "created_at": str(row[6]),
                }
                for row in training_rows
            ],
            "capability_compositions": [
                {
                    "composition_id": str(row[0]),
                    "status": str(row[1]),
                    "implementation_target": str(row[2]),
                    "updated_at": str(row[3]),
                }
                for row in capability_rows
            ],
            "operation_records": [
                {
                    "id": int(row[0]),
                    "model": str(row[1]),
                    "request": json.loads(str(row[2]) or "{}"),
                    "response": json.loads(str(row[3]) or "{}"),
                    "created_at": str(row[4]),
                }
                for row in operation_rows
            ],
        }

    def maintain_language_model(self) -> dict[str, Any]:
        """Audit local training data and compact it without touching other scopes."""

        deactivated: list[int] = []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision, input_text, target_text, quality_score, validation_json
                FROM language_training_example WHERE active = 1
                ORDER BY revision ASC
                """
            ).fetchall()
            for row in rows:
                try:
                    validation = json.loads(str(row[4]) or "{}")
                except json.JSONDecodeError:
                    validation = {}
                facts_preserved = validation.get("facts_preserved")
                if facts_preserved is None:
                    facts_preserved = validation.get("fact_preservation_verified")
                grounding_coverage = validation.get("grounding_coverage")
                if grounding_coverage is None:
                    grounding_coverage = validation.get("semantic_grounding")
                bounded_output = validation.get("bounded_output")
                if bounded_output is None:
                    bounded_output = 8 <= len(str(row[2])) <= 16_000
                valid = (
                    bool(str(row[1]).strip())
                    and bool(str(row[2]).strip())
                    and len(str(row[1])) <= 16_000
                    and len(str(row[2])) <= 16_000
                    and float(row[3]) >= 0.8
                    and isinstance(validation, dict)
                    and facts_preserved is True
                    and bounded_output is True
                    and float(grounding_coverage or 0)
                    >= MIN_TRAINING_GROUNDING_COVERAGE
                )
                if not valid:
                    connection.execute(
                        "UPDATE language_training_example SET active = 0 WHERE revision = ?",
                        (int(row[0]),),
                    )
                    deactivated.append(int(row[0]))
            connection.execute(
                """
                UPDATE language_training_example SET active = 0
                WHERE revision NOT IN (
                    SELECT revision FROM language_training_example
                    WHERE active = 1 ORDER BY revision DESC LIMIT ?
                )
                """,
                (self.MAX_LANGUAGE_TRAINING_EXAMPLES,),
            )
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM language_training_example WHERE active = 1"
                ).fetchone()[0]
            )
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            connection.execute("PRAGMA optimize")
            result = {
                "ok": integrity.casefold() == "ok",
                "owner_model_id": self.owner_model_id,
                "database_scope": self.database_scope,
                "active_example_count": active_count,
                "deactivated_revisions": deactivated,
                "deactivated_count": len(deactivated),
                "sqlite_integrity": integrity,
                "weights_rebuild_required": bool(deactivated),
            }
            run_id = f"star-maintain-{uuid.uuid4().hex[:24]}"
            created_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO language_model_maintenance(
                    run_id, action, result_json, ok, created_at
                ) VALUES (?, 'audit-compact-optimize', ?, ?, ?)
                """,
                (
                    run_id,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    int(result["ok"]),
                    created_at,
                ),
            )
            connection.execute(
                """
                DELETE FROM language_model_maintenance WHERE run_id NOT IN (
                    SELECT run_id FROM language_model_maintenance
                    ORDER BY created_at DESC LIMIT 100
                )
                """
            )
        return {**result, "run_id": run_id, "created_at": created_at}

    def record_internal_training_run(self, result: dict[str, Any]) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        run_id = str(result.get("training_run_id") or f"internal-{uuid.uuid4().hex[:24]}")
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO language_model_maintenance(
                    run_id, action, result_json, ok, created_at
                ) VALUES (?, 'ollama-internal-training', ?, ?, ?)
                """,
                (
                    run_id[:160],
                    json.dumps(result, ensure_ascii=False, sort_keys=True)[:512_000],
                    int(result.get("ok") is True),
                    created_at,
                ),
            )
        return {
            "run_id": run_id,
            "created_at": created_at,
            "ok": result.get("ok") is True,
        }

    def latest_internal_training_run(self) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, result_json, ok, created_at
                FROM language_model_maintenance
                WHERE action = 'ollama-internal-training'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return {}
        try:
            result = json.loads(str(row[1]) or "{}")
        except json.JSONDecodeError:
            result = {}
        return {
            "run_id": str(row[0]),
            "result": result if isinstance(result, dict) else {},
            "ok": bool(row[2]),
            "created_at": str(row[3]),
        }
