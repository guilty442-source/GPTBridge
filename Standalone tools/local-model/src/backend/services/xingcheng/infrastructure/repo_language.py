from __future__ import annotations

import json
from typing import Any


class LanguageTrainingMixin:
    """Inference recording and private-context reads for LocalAiRepository.

    Language training, preference-pair, and maintenance plumbing was retired
    under B167/B38; only the operational inference log and the non-learning
    slices of native private context remain.
    """

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

    def native_private_context(self, *, limit: int = 6) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        bounded_limit = max(1, min(12, int(limit)))
        with self._connect() as connection:
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
