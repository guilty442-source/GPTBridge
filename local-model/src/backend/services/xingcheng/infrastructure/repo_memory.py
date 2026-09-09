from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


class MemoryMixin:
    """Brokered memory storage, retrieval, and review for LocalAiRepository."""

    def store_brokered_memory(
        self,
        *,
        memory_id: str = "",
        kind: str,
        title: str,
        content: str,
        business_scope: str,
        source_type: str,
        source_id: str,
        source_model_id: str,
        broker_model_id: str,
        confidence: float = 0.5,
        expires_at: str = "",
        review_status: str = "approved",
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if str(broker_model_id).strip() != self.MODEL_ID_BY_SCOPE["main"]:
            raise PermissionError("MODEL_MEMORY_BROKER_DENIED")
        scope = str(business_scope or "general").strip().casefold()
        if scope not in {"general", "investment"}:
            raise ValueError("unsupported memory business scope")
        normalized_content = str(content or "").strip()[:4_000]
        if not normalized_content:
            raise ValueError("memory content is required")
        normalized_kind = str(kind or "context").strip()[:64] or "context"
        normalized_title = str(title or normalized_content[:80]).strip()[:160]
        normalized_source_type = str(source_type or "internal").strip()[:64]
        normalized_source_id = str(source_id or "unknown").strip()[:96]
        normalized_source_model = str(source_model_id or "unknown").strip()[:96]
        normalized_confidence = max(0.0, min(1.0, float(confidence)))
        normalized_review_status = str(review_status or "pending-review").strip().casefold()
        if normalized_review_status not in {
            "pending-review",
            "approved",
            "rejected",
            "revoked",
        }:
            raise ValueError("unsupported memory review status")
        provenance_json = json.dumps(
            dict(provenance or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )[:4_000]
        digest = hashlib.sha256(
            json.dumps(
                {
                    "kind": normalized_kind,
                    "content": normalized_content,
                    "scope": scope,
                    "source": normalized_source_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        now = self._utc_now()
        normalized_memory_id = str(memory_id or "").strip()[:64] or uuid.uuid4().hex[:24]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO model_memory(
                    memory_id, content_hash, kind, title, content,
                    business_scope, source_type, source_id, source_model_id,
                    broker_model_id, confidence, expires_at, review_status,
                    reviewed_by, reviewed_at, review_reason, revoked_at,
                    provenance_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '', ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    confidence = MAX(model_memory.confidence, excluded.confidence),
                    expires_at = excluded.expires_at,
                    review_status = CASE
                        WHEN model_memory.review_status = 'approved' THEN 'approved'
                        ELSE excluded.review_status
                    END,
                    provenance_json = excluded.provenance_json,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_memory_id,
                    digest,
                    normalized_kind,
                    normalized_title,
                    normalized_content,
                    scope,
                    normalized_source_type,
                    normalized_source_id,
                    normalized_source_model,
                    broker_model_id,
                    normalized_confidence,
                    str(expires_at or "").strip(),
                    normalized_review_status,
                    provenance_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT memory_id, content_hash, kind, title, content,
                       business_scope, source_type, source_id, source_model_id,
                       broker_model_id, confidence, expires_at, review_status,
                       reviewed_by, reviewed_at, review_reason, revoked_at,
                       provenance_json, created_at, updated_at
                FROM model_memory WHERE content_hash = ?
                """,
                (digest,),
            ).fetchone()
            connection.execute(
                "DELETE FROM model_memory WHERE expires_at <> '' AND expires_at < ?",
                (now,),
            )
            connection.execute(
                """
                DELETE FROM model_memory WHERE memory_id NOT IN (
                    SELECT memory_id FROM model_memory
                    ORDER BY updated_at DESC LIMIT ?
                )
                """,
                (self.MAX_MODEL_MEMORIES,),
            )
        values = tuple(row) if row is not None else ()
        keys = (
            "memory_id",
            "content_hash",
            "kind",
            "title",
            "content",
            "business_scope",
            "source_type",
            "source_id",
            "source_model_id",
            "broker_model_id",
            "confidence",
            "expires_at",
            "review_status",
            "reviewed_by",
            "reviewed_at",
            "review_reason",
            "revoked_at",
            "provenance_json",
            "created_at",
            "updated_at",
        )
        return {
            **dict(zip(keys, values)),
            "owner_model_id": self.owner_model_id,
            "database_shared": False,
        }

    def memory_context(
        self,
        business_scope: str,
        *,
        limit: int = 10,
        include_pending: bool = False,
    ) -> list[dict[str, Any]]:
        scope = str(business_scope or "general").strip().casefold()
        if scope not in {"general", "investment"}:
            raise ValueError("unsupported memory business scope")
        now = self._utc_now()
        with self._connect() as connection:
            status_filter = "review_status IN ('approved', 'pending-review')" if include_pending else "review_status = 'approved'"
            rows = connection.execute(
                f"""
                SELECT memory_id, kind, title, content, business_scope,
                       source_model_id, confidence, updated_at, review_status,
                       source_type, source_id, expires_at, provenance_json
                FROM model_memory
                WHERE business_scope IN ('general', ?)
                  AND (expires_at = '' OR expires_at >= ?)
                  AND revoked_at = ''
                  AND {status_filter}
                ORDER BY confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (scope, now, max(1, min(20, int(limit)))),
            ).fetchall()
        return [
            {
                "memory_id": str(row[0]),
                "kind": str(row[1]),
                "title": str(row[2]),
                "content": str(row[3]),
                "business_scope": str(row[4]),
                "source_model_id": str(row[5]),
                "origin_model_id": self.owner_model_id,
                "confidence": float(row[6]),
                "updated_at": str(row[7]),
                "review_status": str(row[8]),
                "source_type": str(row[9]),
                "source_id": str(row[10]),
                "expires_at": str(row[11]),
                "provenance": json.loads(str(row[12]) or "{}"),
            }
            for row in rows
        ]

    def review_memory(
        self,
        memory_id: str,
        *,
        action: str,
        reviewer: str,
        reason: str = "",
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip().casefold()
        target_status = {
            "approve": "approved",
            "reject": "rejected",
            "revoke": "revoked",
        }.get(normalized_action)
        if target_status is None:
            raise ValueError("memory review action must be approve, reject, or revoke")
        normalized_reviewer = str(reviewer or "").strip()[:96]
        if not normalized_reviewer:
            raise ValueError("memory reviewer is required")
        now = self._utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE model_memory
                SET review_status = ?, reviewed_by = ?, reviewed_at = ?,
                    review_reason = ?, revoked_at = ?, updated_at = ?
                WHERE memory_id = ?
                """,
                (
                    target_status,
                    normalized_reviewer,
                    now,
                    str(reason or "").strip()[:1_000],
                    now if target_status == "revoked" else "",
                    now,
                    str(memory_id or "").strip(),
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("memory not found")
        records = self.memory_records(include_inactive=True, limit=500)
        return next(item for item in records if item["memory_id"] == memory_id)

    def memory_records(
        self,
        *,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "" if include_inactive else "WHERE review_status IN ('pending-review', 'approved') AND revoked_at = ''"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT memory_id, kind, title, content, business_scope,
                       source_type, source_id, source_model_id, broker_model_id,
                       confidence, expires_at, review_status, reviewed_by,
                       reviewed_at, review_reason, revoked_at, provenance_json,
                       created_at, updated_at
                FROM model_memory {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ).fetchall()
        keys = (
            "memory_id", "kind", "title", "content", "business_scope",
            "source_type", "source_id", "source_model_id", "broker_model_id",
            "confidence", "expires_at", "review_status", "reviewed_by",
            "reviewed_at", "review_reason", "revoked_at", "provenance",
            "created_at", "updated_at",
        )
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(zip(keys, row))
            try:
                item["provenance"] = json.loads(str(item["provenance"] or "{}"))
            except json.JSONDecodeError:
                item["provenance"] = {}
            item["owner_model_id"] = self.owner_model_id
            output.append(item)
        return output
