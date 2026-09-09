"""Repair learning engine — records errors, learns from outcomes, auto-upgrades recipes.

Architecture:

  ErrorSignature → RepairOutcome → PatternAnalysis → LearnedRecipe → SelfUpgrade

  1. ErrorSignature: normalized fingerprint of a failure (error class, message
     pattern, file context, failure_code).
  2. RepairOutcome: what repair action was attempted and whether it succeeded.
  3. PatternAnalysis: recurring error→remedy pairs extracted from history.
  4. LearnedRecipe: auto-generated recipe from a verified pattern.
  5. SelfUpgrade: learned recipes are merged into the knowledge base and
     become available for future repair dispatch.

All learning is persisted in the existing automatic-repair SQLite database
under a dedicated `repair_learning` schema.  The learner never weakens
governance or safety boundaries — it only adds new *remedy hints* that the
existing repair pipeline may consult.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

REPAIR_LEARNING_VERSION: Final[str] = "1.00000"

# Minimum occurrences of an error→remedy pair before promoting to a recipe.
LEARN_PROMOTION_THRESHOLD: Final[int] = 2
# Maximum learned recipes to retain (LRU eviction).
MAX_LEARNED_RECIPES: Final[int] = 50


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_error_signature(
    error_class: str,
    message: str,
    *,
    file_path: str = "",
) -> str:
    """Produce a stable hash from error class + normalized message + file basename."""
    # Strip variable parts: line numbers, hex IDs, timestamps, absolute paths.
    normalized_message = re.sub(r"\d+", "#", message)
    normalized_message = re.sub(r"0x[0-9a-fA-F]+", "0xH", normalized_message)
    normalized_message = re.sub(
        r"[a-f0-9]{8,}", "HASH", normalized_message
    )
    normalized_message = re.sub(
        r"[A-Za-z]:\\[^:\s]+", "PATH", normalized_message
    )
    normalized_message = re.sub(r"/[^:\s]+", "PATH", normalized_message)
    file_basename = Path(file_path).name if file_path else ""
    raw = f"{error_class}|{normalized_message}|{file_basename}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ErrorSignature:
    """Normalized fingerprint of a failure."""

    signature_hash: str
    error_class: str
    message_pattern: str
    failure_code: str
    file_context: str = ""
    target_tool_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairOutcome:
    """Record of one repair attempt."""

    run_id: str
    signature_hash: str
    remedy: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LearnedRecipe:
    """A recipe auto-generated from observed repair patterns."""

    recipe_id: str
    name: str
    failure_signatures: tuple[str, ...]
    remedy: str
    owner: str
    automatic: bool = True
    runtime_only: bool = True
    learned_at: str = ""
    occurrence_count: int = 0
    success_rate: float = 0.0
    source: str = "learned"

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "failure_signatures": list(self.failure_signatures),
            "remedy": self.remedy,
            "owner": self.owner,
            "automatic": self.automatic,
            "runtime_only": self.runtime_only,
            "learned_at": self.learned_at,
            "occurrence_count": self.occurrence_count,
            "success_rate": self.success_rate,
            "source": self.source,
        }


class RepairLearningStore:
    """SQLite-backed store for error signatures, outcomes, and learned recipes."""

    SCHEMA_VERSION = 1

    def __init__(self, database_root: Path) -> None:
        self.database_root = database_root.resolve()
        self._path = self.database_root / "repair-learning.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.database_root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS error_signatures ("
            "signature_hash TEXT PRIMARY KEY, "
            "error_class TEXT NOT NULL, "
            "message_pattern TEXT NOT NULL, "
            "failure_code TEXT NOT NULL, "
            "file_context TEXT NOT NULL DEFAULT '', "
            "target_tool_id TEXT NOT NULL DEFAULT '', "
            "first_seen TEXT NOT NULL, "
            "last_seen TEXT NOT NULL, "
            "occurrence_count INTEGER NOT NULL DEFAULT 1)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS repair_outcomes ("
            "outcome_id TEXT PRIMARY KEY, "
            "run_id TEXT NOT NULL, "
            "signature_hash TEXT NOT NULL, "
            "remedy TEXT NOT NULL, "
            "ok INTEGER NOT NULL, "
            "detail_json TEXT NOT NULL DEFAULT '{}', "
            "recorded_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS learned_recipes ("
            "recipe_id TEXT PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "failure_signatures_json TEXT NOT NULL, "
            "remedy TEXT NOT NULL, "
            "owner TEXT NOT NULL DEFAULT 'main-system', "
            "automatic INTEGER NOT NULL DEFAULT 1, "
            "runtime_only INTEGER NOT NULL DEFAULT 1, "
            "learned_at TEXT NOT NULL, "
            "occurrence_count INTEGER NOT NULL DEFAULT 0, "
            "success_rate REAL NOT NULL DEFAULT 0.0, "
            "source TEXT NOT NULL DEFAULT 'learned')"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_outcomes_signature "
            "ON repair_outcomes(signature_hash)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_outcomes_remedy "
            "ON repair_outcomes(remedy, ok)"
        )
        return connection

    def record_error(self, signature: ErrorSignature) -> None:
        """Insert or update an error signature occurrence."""
        connection = self._connect()
        try:
            now = _iso_now()
            existing = connection.execute(
                "SELECT occurrence_count FROM error_signatures WHERE signature_hash = ?",
                (signature.signature_hash,),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE error_signatures SET last_seen = ?, occurrence_count = ? "
                    "WHERE signature_hash = ?",
                    (now, existing[0] + 1, signature.signature_hash),
                )
            else:
                connection.execute(
                    "INSERT INTO error_signatures "
                    "(signature_hash, error_class, message_pattern, failure_code, "
                    "file_context, target_tool_id, first_seen, last_seen, occurrence_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        signature.signature_hash,
                        signature.error_class,
                        signature.message_pattern,
                        signature.failure_code,
                        signature.file_context,
                        signature.target_tool_id,
                        now,
                        now,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def record_outcome(self, outcome: RepairOutcome) -> None:
        """Record a repair attempt outcome."""
        connection = self._connect()
        try:
            outcome_id = uuid4().hex
            if not outcome.recorded_at:
                outcome.recorded_at = _iso_now()
            connection.execute(
                "INSERT INTO repair_outcomes "
                "(outcome_id, run_id, signature_hash, remedy, ok, detail_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    outcome_id,
                    outcome.run_id,
                    outcome.signature_hash,
                    outcome.remedy,
                    int(outcome.ok),
                    json.dumps(outcome.detail, ensure_ascii=False),
                    outcome.recorded_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def get_outcomes_for_signature(self, signature_hash: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT run_id, remedy, ok, detail_json, recorded_at "
                "FROM repair_outcomes WHERE signature_hash = ? "
                "ORDER BY recorded_at DESC",
                (signature_hash,),
            ).fetchall()
            return [
                {
                    "run_id": r[0],
                    "remedy": r[1],
                    "ok": bool(r[2]),
                    "detail": json.loads(r[3] or "{}"),
                    "recorded_at": r[4],
                }
                for r in rows
            ]
        finally:
            connection.close()

    def get_all_error_signatures(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT signature_hash, error_class, message_pattern, failure_code, "
                "file_context, target_tool_id, first_seen, last_seen, occurrence_count "
                "FROM error_signatures ORDER BY occurrence_count DESC"
            ).fetchall()
            return [
                {
                    "signature_hash": r[0],
                    "error_class": r[1],
                    "message_pattern": r[2],
                    "failure_code": r[3],
                    "file_context": r[4],
                    "target_tool_id": r[5],
                    "first_seen": r[6],
                    "last_seen": r[7],
                    "occurrence_count": r[8],
                }
                for r in rows
            ]
        finally:
            connection.close()

    def save_learned_recipe(self, recipe: LearnedRecipe) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO learned_recipes "
                "(recipe_id, name, failure_signatures_json, remedy, owner, "
                "automatic, runtime_only, learned_at, occurrence_count, "
                "success_rate, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recipe.recipe_id,
                    recipe.name,
                    json.dumps(list(recipe.failure_signatures)),
                    recipe.remedy,
                    recipe.owner,
                    int(recipe.automatic),
                    int(recipe.runtime_only),
                    recipe.learned_at or _iso_now(),
                    recipe.occurrence_count,
                    recipe.success_rate,
                    recipe.source,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def get_learned_recipes(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT recipe_id, name, failure_signatures_json, remedy, owner, "
                "automatic, runtime_only, learned_at, occurrence_count, "
                "success_rate, source "
                "FROM learned_recipes ORDER BY occurrence_count DESC"
            ).fetchall()
            return [
                {
                    "recipe_id": r[0],
                    "name": r[1],
                    "failure_signatures": json.loads(r[2] or "[]"),
                    "remedy": r[3],
                    "owner": r[4],
                    "automatic": bool(r[5]),
                    "runtime_only": bool(r[6]),
                    "learned_at": r[7],
                    "occurrence_count": r[8],
                    "success_rate": r[9],
                    "source": r[10],
                }
                for r in rows
            ]
        finally:
            connection.close()

    def evict_oldest_if_needed(self, max_recipes: int = MAX_LEARNED_RECIPES) -> int:
        """Evict oldest learned recipes beyond the retention limit. Returns count evicted."""
        connection = self._connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM learned_recipes"
            ).fetchone()[0]
            if count <= max_recipes:
                return 0
            to_evict = count - max_recipes
            connection.execute(
                "DELETE FROM learned_recipes WHERE recipe_id IN ("
                "SELECT recipe_id FROM learned_recipes "
                "ORDER BY learned_at ASC LIMIT ?)",
                (to_evict,),
            )
            connection.commit()
            return to_evict
        finally:
            connection.close()


class RepairLearner:
    """Analyzes repair history and auto-generates recipes from patterns."""

    def __init__(self, store: RepairLearningStore) -> None:
        self.store = store

    def learn_from_outcome(
        self,
        signature: ErrorSignature,
        outcome: RepairOutcome,
    ) -> dict[str, Any]:
        """Record an error + outcome, then attempt pattern promotion."""
        self.store.record_error(signature)
        self.store.record_outcome(outcome)
        return self._try_promote_pattern(signature.signature_hash)

    def _try_promote_pattern(self, signature_hash: str) -> dict[str, Any]:
        """Check if an error→remedy pair has occurred enough times to promote."""
        outcomes = self.store.get_outcomes_for_signature(signature_hash)
        if len(outcomes) < LEARN_PROMOTION_THRESHOLD:
            return {
                "promoted": False,
                "reason": f"only {len(outcomes)} occurrences; need {LEARN_PROMOTION_THRESHOLD}",
            }
        # Find the most successful remedy.
        remedy_stats: dict[str, dict[str, int]] = {}
        for o in outcomes:
            remedy = o["remedy"]
            if remedy not in remedy_stats:
                remedy_stats[remedy] = {"total": 0, "success": 0}
            remedy_stats[remedy]["total"] += 1
            if o["ok"]:
                remedy_stats[remedy]["success"] += 1
        best_remedy = ""
        best_success_rate = 0.0
        best_count = 0
        for remedy, stats in remedy_stats.items():
            rate = stats["success"] / stats["total"] if stats["total"] else 0.0
            if rate > best_success_rate or (rate == best_success_rate and stats["total"] > best_count):
                best_remedy = remedy
                best_success_rate = rate
                best_count = stats["total"]
        if not best_remedy or best_success_rate == 0.0:
            return {
                "promoted": False,
                "reason": "no successful remedy found in history",
            }
        # Get the error signature details.
        signatures = {
            s["signature_hash"]: s
            for s in self.store.get_all_error_signatures()
        }
        sig = signatures.get(signature_hash)
        if not sig:
            return {"promoted": False, "reason": "signature not found"}
        recipe_id = f"learned-{signature_hash}"
        recipe = LearnedRecipe(
            recipe_id=recipe_id,
            name=f"Learned repair for {sig['error_class']} in {sig.get('file_context', 'unknown')}",
            failure_signatures=(sig["error_class"], sig["failure_code"]),
            remedy=best_remedy,
            owner="main-system",
            automatic=True,
            runtime_only=True,
            learned_at=_iso_now(),
            occurrence_count=best_count,
            success_rate=best_success_rate,
            source="learned",
        )
        self.store.save_learned_recipe(recipe)
        evicted = self.store.evict_oldest_if_needed()
        return {
            "promoted": True,
            "recipe": recipe.as_dict(),
            "evicted_oldest": evicted,
        }

    def suggest_remedy(self, signature: ErrorSignature) -> dict[str, Any]:
        """Look up the best known remedy for an error signature."""
        outcomes = self.store.get_outcomes_for_signature(signature.signature_hash)
        if not outcomes:
            return {"suggested": False, "reason": "no history for this signature"}
        remedy_stats: dict[str, dict[str, int]] = {}
        for o in outcomes:
            remedy = o["remedy"]
            if remedy not in remedy_stats:
                remedy_stats[remedy] = {"total": 0, "success": 0}
            remedy_stats[remedy]["total"] += 1
            if o["ok"]:
                remedy_stats[remedy]["success"] += 1
        best_remedy = ""
        best_rate = 0.0
        for remedy, stats in remedy_stats.items():
            rate = stats["success"] / stats["total"] if stats["total"] else 0.0
            if rate > best_rate:
                best_remedy = remedy
                best_rate = rate
        if not best_remedy:
            return {"suggested": False, "reason": "no successful remedy in history"}
        return {
            "suggested": True,
            "remedy": best_remedy,
            "success_rate": best_rate,
            "occurrence_count": len(outcomes),
        }

    def analyze_history(self) -> dict[str, Any]:
        """Full analysis of repair history for learning insights."""
        signatures = self.store.get_all_error_signatures()
        learned = self.store.get_learned_recipes()
        total_errors = sum(s["occurrence_count"] for s in signatures)
        recurring = [s for s in signatures if s["occurrence_count"] >= LEARN_PROMOTION_THRESHOLD]
        return {
            "version": REPAIR_LEARNING_VERSION,
            "total_error_types": len(signatures),
            "total_error_occurrences": total_errors,
            "recurring_errors": len(recurring),
            "learned_recipes": len(learned),
            "top_errors": recurring[:5],
            "learned_recipe_ids": [r["recipe_id"] for r in learned],
        }


__all__ = [
    "ErrorSignature",
    "LearnedRecipe",
    "LEARN_PROMOTION_THRESHOLD",
    "MAX_LEARNED_RECIPES",
    "REPAIR_LEARNING_VERSION",
    "RepairLearner",
    "RepairLearningStore",
    "RepairOutcome",
    "_normalize_error_signature",
]
