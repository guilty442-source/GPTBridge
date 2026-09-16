"""Repair learning store — learning-system-sovereign: verified repeatable recipes.

INPUT: typed-errors+repair-outcomes+verification-results
LEARN: normalized-signature+success-rate+bounded-recipe
AUTOMATION: verified-repeatable-recipes-only
EXECUTION: maintenance-governed-executor
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    LearnedRecipe,
    VerificationResult,
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
        self._db_path = repair_root / "auto-repair-learning.sqlite3"
        self._init_db()

    def _init_db(self) -> None:
        self.repair_root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS repair_outcomes (
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
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learned_recipes (
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
                )
            """)
            conn.commit()

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
        with sqlite3.connect(self._db_path, timeout=10) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO repair_outcomes
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
            conn.commit()

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
                   SUM(verification_result = 'passed') as passed_count
            FROM repair_outcomes
        """
        params: tuple[Any, ...] = ()
        if signature_hash:
            query += " WHERE signature_hash = ?"
            params = (signature_hash,)
        query += """
            GROUP BY signature_hash, remedy
            HAVING count >= 3 AND successes * 1.0 / count >= 0.8
               AND passed_count > 0
        """
        with sqlite3.connect(self._db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
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
        with sqlite3.connect(self._db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT recipe_id FROM learned_recipes WHERE signature_hash = ? AND remedy = ?",
                (signature_hash, remedy),
            ).fetchone()
        return row[0] if row else None

    def _refresh_recipe(self, recipe_id: str, candidate: dict[str, Any]) -> LearnedRecipe:
        """Refresh an already-promoted recipe instead of duplicating it."""
        proof = {"promotion_criteria": "success_rate>=0.8,verified=passed,count>=3"}
        promoted_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path, timeout=10) as conn:
            conn.execute(
                """
                UPDATE learned_recipes
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
            conn.commit()
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

        with sqlite3.connect(self._db_path, timeout=10) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO learned_recipes
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
            conn.commit()

        self.audit.record("recipe_promoted", {
            "recipe_id": recipe.recipe_id,
            "signature_hash": recipe.signature_hash,
            "success_rate": recipe.success_rate,
        })

        return recipe

    def get_learned_recipes(self) -> list[dict[str, Any]]:
        """Get all learned recipes."""
        with sqlite3.connect(self._db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT recipe_id, signature_hash, error_class, message_pattern, "
                "remedy, success_rate, occurrence_count, verification_proof_json, "
                "promoted_at, promoted_by "
                "FROM learned_recipes ORDER BY promoted_at DESC"
            )
            return [dict(row) for row in cursor]


__all__ = ["RepairLearningStore"]
