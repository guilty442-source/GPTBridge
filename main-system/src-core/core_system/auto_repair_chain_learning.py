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
        self._db_path = repair_root / "repair-learning.sqlite3"
        self._init_db()

    def _init_db(self) -> None:
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

    def analyze_history(self) -> dict[str, Any]:
        """Analyze repair history for promotion candidates."""
        with sqlite3.connect(self._db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT signature_hash, error_class, message_pattern, remedy,
                       COUNT(*) as count,
                       SUM(ok) as successes,
                       MAX(verification_result) as max_verification
                FROM repair_outcomes
                GROUP BY signature_hash, remedy
                HAVING count >= 3 AND successes * 1.0 / count >= 0.8
            """)
            candidates = []
            for row in cursor:
                if row["max_verification"] == "passed":
                    candidates.append({
                        "signature_hash": row["signature_hash"],
                        "error_class": row["error_class"],
                        "message_pattern": row["message_pattern"],
                        "remedy": row["remedy"],
                        "success_rate": row["successes"] / row["count"],
                        "occurrence_count": row["count"],
                    })
            return {"candidates": candidates, "total_error_types": len(candidates)}

    def promote_recipe(self, candidate: dict[str, Any]) -> LearnedRecipe:
        """Promote verified recipe to learned recipes."""
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
            cursor = conn.execute("SELECT * FROM learned_recipes ORDER BY promoted_at DESC")
            return [dict(row) for row in cursor]


__all__ = ["RepairLearningStore"]
