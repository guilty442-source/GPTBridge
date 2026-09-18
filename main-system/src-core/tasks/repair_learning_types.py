"""Repair learning — types, constants, and helpers.

Extracted from repair_learning.py: dataclasses (ErrorSignature,
RepairOutcome, LearnedRecipe), constants, schema statements,
and the error-signature normalization helper.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from core_system.versioning import component_version

REPAIR_LEARNING_VERSION: Final[str] = component_version("repair-learning")

# Minimum occurrences of an error→remedy pair before promoting to a recipe.
LEARN_PROMOTION_THRESHOLD: Final[int] = 2
# Minimum success rate a remedy must reach before it may become an automatic
# recipe.  Without this floor a remedy that almost never works (for example a
# passive watchdog remedy for a fault that keeps recurring) would still be
# promoted and then auto-planned by the repair chain.
LEARN_PROMOTION_MIN_SUCCESS_RATE: Final[float] = 0.8
# Maximum learned recipes to retain (LRU eviction).
MAX_LEARNED_RECIPES: Final[int] = 50

# Provenance marker for recipes taught through the governed ``learn.teach``
# command — distinct from ``learned`` (promoted by verified outcomes) and
# the static REPAIR_RECIPES curriculum baked into code.
TAUGHT_RECIPE_SOURCE: Final[str] = "taught"

# Remedy tokens a taught recipe may carry.  Teaching is bounded to the
# same runtime-safe action vocabulary the learned-recipe merge accepts —
# taught knowledge may never promote a source mutation
# (``repair-main-system-source``) into an automatic plan.
TEACHABLE_REMEDY_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "inspect-owned-databases",
        "rebuild-tool-executable",
        "no-action-required",
    }
)


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
    # Declared verification statement for ``source == "taught"`` recipes —
    # the same contract the static REPAIR_RECIPES carry (what must hold
    # after execution), not outcome-earned proof.
    verification: str = ""

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
            "verification": self.verification,
        }


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS error_signatures ("
    "signature_hash TEXT PRIMARY KEY, "
    "error_class TEXT NOT NULL, "
    "message_pattern TEXT NOT NULL, "
    "failure_code TEXT NOT NULL, "
    "file_context TEXT NOT NULL DEFAULT '', "
    "target_tool_id TEXT NOT NULL DEFAULT '', "
    "first_seen TEXT NOT NULL, "
    "last_seen TEXT NOT NULL, "
    "occurrence_count INTEGER NOT NULL DEFAULT 1)",
    "CREATE TABLE IF NOT EXISTS repair_outcomes ("
    "outcome_id TEXT PRIMARY KEY, "
    "run_id TEXT NOT NULL, "
    "signature_hash TEXT NOT NULL, "
    "remedy TEXT NOT NULL, "
    "ok INTEGER NOT NULL, "
    "detail_json TEXT NOT NULL DEFAULT '{}', "
    "recorded_at TEXT NOT NULL)",
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
    "source TEXT NOT NULL DEFAULT 'learned', "
    "verification TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_signature "
    "ON repair_outcomes(signature_hash)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_remedy "
    "ON repair_outcomes(remedy, ok)",
)
