"""Repair learning store — learning-system-sovereign: verified repeatable recipes.

INPUT: typed-errors+repair-outcomes+verification-results
LEARN: normalized-signature+success-rate+bounded-recipe
AUTOMATION: verified-repeatable-recipes-only
EXECUTION: maintenance-governed-executor

A610/A621: PostgreSQL is the sole structured-data authority. The retired
``auto-repair-learning.sqlite3`` store is superseded by the
``gptbridge_repair`` schema; chain-learning rows live in
``chain_repair_outcomes`` / ``chain_learned_recipes`` (the unprefixed
names belong to ``tasks.repair_learning``).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from shared_layer.local import pg_adapter

from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    LearnedRecipe,
    VerificationResult,
)

PG_SCHEMA = "gptbridge_repair"

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS chain_repair_outcomes (
        run_id TEXT PRIMARY KEY,
        signature_hash TEXT NOT NULL,
        error_class TEXT NOT NULL,
        message_pattern TEXT,
        failure_code TEXT,
        remedy TEXT NOT NULL,
        ok INTEGER NOT NULL,
        verification_result TEXT,
        detail_json TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS chain_learned_recipes (
        recipe_id TEXT PRIMARY KEY,
        signature_hash TEXT NOT NULL,
        error_class TEXT NOT NULL,
        message_pattern TEXT,
        remedy TEXT NOT NULL,
        success_rate REAL NOT NULL,
        occurrence_count INTEGER NOT NULL,
        verification_proof_json TEXT NOT NULL,
        promoted_at TEXT NOT NULL,
        promoted_by TEXT NOT NULL
    )""",
)


class RepairLearningStore:
    """Learning-system-sovereign: learns verified repeatable recipes.

    INPUT: typed-errors+repair-outcomes+verification-results
    LEARN: normalized-signature+success-rate+bounded-recipe
    AUTOMATION: verified-repeatable-recipes-only
    EXECUTION: maintenance-governed-executor
    """

    def __init__(self, repair_root: Path, audit: GovernanceAudit):
        self.repair_root = repair_root
        self.audit = audit
        self._db_path = f"postgresql:{PG_SCHEMA}"
        self._init_db()

    def _init_db(self) -> None:
        with pg_adapter.connect(PG_SCHEMA) as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)  # sql-ok: idempotent DDL bootstrap

    def record_outcome(
        self,
        signature_hash: str,
        error_class: str,
        message_pattern: str,
        failure_code: str,
        remedy: str,
        ok: bool,
        verification_result: Optional[VerificationResult],
        detail: dict[str, Any],
    ) -> None:
        """Record repair outcome for learning."""
        with pg_adapter.connect(PG_SCHEMA) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO chain_repair_outcomes
                (run_id, signature_hash, error_class, message_pattern, failure_code, remedy, ok, verification_result, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                uuid.uuid4().hex,
                signature_hash,
                error_class,
                message_pattern,
                failure_code,
                remedy,
                int(ok),
                verification_result.value if verification_result else "none",
                json.dumps(detail, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ))

    def analyze_history(self, signature_hash: str | None = None) -> dict[str, Any]:
        """Analyze repair history for promotion candidates.

        ``signature_hash`` scopes the aggregate to one fault signature so
        the orchestrator does not re-scan unrelated history after every
        repair.
        """
        query = """
            SELECT signature_hash, error_class, message_pattern, remedy,
                   COUNT(*) as count,
                   SUM(ok) as successes,
                   SUM(CASE WHEN verification_result = 'passed' THEN 1 ELSE 0 END) as passed_count
            FROM chain_repair_outcomes
        """
        params: tuple[Any, ...] = ()
        if signature_hash:
            query += " WHERE signature_hash = ?"
            params = (signature_hash,)
        query += """
            GROUP BY signature_hash, error_class, message_pattern, remedy
            HAVING COUNT(*) >= 3
               AND SUM(ok) * 1.0 / COUNT(*) >= 0.8
               AND SUM(CASE WHEN verification_result = 'passed' THEN 1 ELSE 0 END) > 0
        """
        with pg_adapter.connect(PG_SCHEMA) as conn:
            cursor = conn.execute(query, params)
            candidates = [
                {
                    "signature_hash": row["signature_hash"],
                    "error_class": row["error_class"],
                    "message_pattern": row["message_pattern"],
                    "remedy": row["remedy"],
                    "success_rate": row["successes"] / row["count"],
                    "occurrence_count": row["count"],
                }
                for row in cursor
            ]
            return {"candidates": candidates, "total_error_types": len(candidates)}

    def _existing_recipe_id(self, signature_hash: str, remedy: str) -> Optional[str]:
        """Return the recipe_id already promoted for this signature+remedy."""
        with pg_adapter.connect(PG_SCHEMA) as conn:
            row = conn.execute(
                "SELECT recipe_id FROM chain_learned_recipes WHERE signature_hash = ? AND remedy = ?",
                (signature_hash, remedy),
            ).fetchone()
        return row[0] if row else None

    def _refresh_recipe(self, recipe_id: str, candidate: dict[str, Any]) -> LearnedRecipe:
        """Refresh an already-promoted recipe instead of duplicating it."""
        proof = {"promotion_criteria": "success_rate>=0.8,verified=passed,count>=3"}
        promoted_at = datetime.now(timezone.utc).isoformat()
        with pg_adapter.connect(PG_SCHEMA) as conn:
            conn.execute(
                """
                UPDATE chain_learned_recipes
                SET success_rate = ?, occurrence_count = ?,
                    verification_proof_json = ?, promoted_at = ?
                WHERE recipe_id = ?
                """,
                (
                    candidate["success_rate"],
                    candidate["occurrence_count"],
                    json.dumps(proof, ensure_ascii=False),
                    promoted_at,
                    recipe_id,
                ),
            )
        return LearnedRecipe(
            recipe_id=recipe_id,
            signature_hash=candidate["signature_hash"],
            error_class=candidate["error_class"],
            message_pattern=candidate["message_pattern"],
            remedy=candidate["remedy"],
            success_rate=candidate["success_rate"],
            occurrence_count=candidate["occurrence_count"],
            verification_proof=proof,
            promoted_at=promoted_at,
        )

    def promote_recipe(self, candidate: dict[str, Any]) -> LearnedRecipe:
        """Promote verified recipe to learned recipes.

        Idempotent per ``signature_hash``+``remedy``: a recipe that was
        already promoted is refreshed (rate/count/proof) rather than
        duplicated.
        """
        existing_id = self._existing_recipe_id(
            candidate["signature_hash"], candidate["remedy"]
        )
        if existing_id is not None:
            return self._refresh_recipe(existing_id, candidate)

        recipe = LearnedRecipe(
            recipe_id=f"learned_{uuid.uuid4().hex[:12]}",
            signature_hash=candidate["signature_hash"],
            error_class=candidate["error_class"],
            message_pattern=candidate["message_pattern"],
            remedy=candidate["remedy"],
            success_rate=candidate["success_rate"],
            occurrence_count=candidate["occurrence_count"],
            verification_proof={"promotion_criteria": "success_rate>=0.8,verified=passed,count>=3"},
            promoted_at=datetime.now(timezone.utc).isoformat(),
        )

        with pg_adapter.connect(PG_SCHEMA) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO chain_learned_recipes
                (recipe_id, signature_hash, error_class, message_pattern, remedy, success_rate, occurrence_count, verification_proof_json, promoted_at, promoted_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                recipe.recipe_id,
                recipe.signature_hash,
                recipe.error_class,
                recipe.message_pattern,
                recipe.remedy,
                recipe.success_rate,
                recipe.occurrence_count,
                json.dumps(recipe.verification_proof, ensure_ascii=False),
                recipe.promoted_at,
                recipe.promoted_by,
            ))

        self.audit.record("recipe_promoted", {
            "recipe_id": recipe.recipe_id,
            "signature_hash": recipe.signature_hash,
            "success_rate": recipe.success_rate,
        })

        return recipe

    def get_learned_recipes(self) -> list[dict[str, Any]]:
        """Get all learned recipes."""
        with pg_adapter.connect(PG_SCHEMA) as conn:
            cursor = conn.execute(
                "SELECT recipe_id, signature_hash, error_class, message_pattern, "
                "remedy, success_rate, occurrence_count, verification_proof_json, "
                "promoted_at, promoted_by "
                "FROM chain_learned_recipes ORDER BY promoted_at DESC"
            )
            return [dict(row) for row in cursor]


__all__ = ["RepairLearningStore"]
