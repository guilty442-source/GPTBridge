from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


class CapabilityMixin:
    """Code upgrade proposals and capability composition storage for LocalAiRepository."""

    def store_code_upgrade_proposal(
        self,
        *,
        target_path: str,
        language: str,
        source_text: str,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        if self.database_scope != "coding":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        target = str(target_path or "").strip()[:1_000]
        normalized_language = str(language or "").strip().casefold()[:32]
        source = str(source_text or "")[:128_000]
        if not target or not source or validation.get("ok") is not True:
            raise ValueError("a validated code upgrade proposal is required")
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        proposal_id = f"star-upgrade-{source_sha256[:24]}"
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO code_upgrade_proposal(
                    proposal_id, target_path, language, source_text,
                    source_sha256, validation_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    proposal_id,
                    target,
                    normalized_language,
                    source,
                    source_sha256,
                    json.dumps(validation, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            inserted = cursor.rowcount > 0
            row = connection.execute(
                """
                SELECT revision, proposal_id, target_path, language,
                       source_sha256, status, created_at
                FROM code_upgrade_proposal
                WHERE target_path = ? AND source_sha256 = ?
                """,
                (target, source_sha256),
            ).fetchone()
        if row is None:
            raise RuntimeError("code upgrade proposal was not stored")
        return {
            "revision": int(row[0]),
            "proposal_id": str(row[1]),
            "target_path": str(row[2]),
            "language": str(row[3]),
            "source_sha256": str(row[4]),
            "status": str(row[5]),
            "created_at": str(row[6]),
            "inserted": inserted,
            "owner_model_id": self.owner_model_id,
        }

    def store_capability_composition(
        self, composition: dict[str, Any]
    ) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        composition_id = str(composition.get("composition_id") or "").strip()[:160]
        if not composition_id:
            raise ValueError("composition_id is required")
        discussion = composition.get("model_discussion")
        discussion = discussion if isinstance(discussion, dict) else {}
        assignments = composition.get("model_assignments")
        assignments = assignments if isinstance(assignments, dict) else {}
        status = str(composition.get("status") or "proposed").strip()[:64]
        target = str(composition.get("implementation_target") or "")[:1_000]
        source_sha256 = str(composition.get("source_sha256") or "")[:64]
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(composition, ensure_ascii=False, sort_keys=True)[:512_000]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO capability_composition(
                    composition_id, status, blueprint_json,
                    model_assignments_json, votes_json, inspections_json,
                    implementation_target, source_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(composition_id) DO UPDATE SET
                    status = excluded.status,
                    blueprint_json = excluded.blueprint_json,
                    model_assignments_json = excluded.model_assignments_json,
                    votes_json = excluded.votes_json,
                    inspections_json = excluded.inspections_json,
                    implementation_target = excluded.implementation_target,
                    source_sha256 = excluded.source_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    composition_id,
                    status,
                    encoded,
                    json.dumps(assignments, ensure_ascii=False, sort_keys=True),
                    json.dumps(discussion.get("voters") or [], ensure_ascii=False),
                    json.dumps(
                        discussion.get("inspection_results") or [], ensure_ascii=False
                    ),
                    target,
                    source_sha256,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT revision, status, created_at, updated_at
                FROM capability_composition WHERE composition_id = ?
                """,
                (composition_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("capability composition was not stored")
        return {
            "revision": int(row[0]),
            "composition_id": composition_id,
            "status": str(row[1]),
            "created_at": str(row[2]),
            "updated_at": str(row[3]),
            "owner_model_id": self.owner_model_id,
            "database_scope": self.database_scope,
        }
