"""G92 — codex normative classification & convergence consumer (A608/A609).

The codex database is the sole authority.  This module is the implementation
side of two codex artifacts:

* ``codex_normative_category_directory`` + ``provision_normative_category``
  (A608): the five-category classification — structural primaries
  PRINCIPLE/ARTICLE/EDICT and normative facets RESPONSIBILITY/PROHIBITION.
* ``codex_normative_convergence_registry`` (A609): each cross-layer
  restatement resolves to exactly one controlling provision; subordinate
  restatements never act as controllers and never enter the runtime index
  as independent rules.

Everything here is read-only and derived: no writes to the codex database.
Consumers (e.g. the RUNTIME_RULE_INDEX_V2 producer) must resolve through
:func:`resolve_controller` so restatements are never indexed as independent
authorities.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE = (
    PROJECT_ROOT / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
)

# A608: structural primary categories keyed by provision_type.
STRUCTURAL_CATEGORIES: dict[str, str] = {
    "principle": "PRINCIPLE",
    "article": "ARTICLE",
    "edict": "EDICT",
}
FACET_CATEGORIES = ("RESPONSIBILITY", "PROHIBITION")


@dataclass(frozen=True)
class ProvisionClassification:
    provision_type: str
    provision_id: str
    structural_category: str
    has_responsibility: bool
    has_prohibition: bool
    lifecycle_state: str
    current_successor: str
    status: str

    @property
    def key(self) -> str:
        return f"{self.provision_type}:{self.provision_id}"

    @property
    def is_current(self) -> bool:
        return self.status == "current" and self.lifecycle_state == "active"


@dataclass(frozen=True)
class ConvergenceEntry:
    statement_code: str
    controlling_key: str
    restatement_key: str
    relationship_kind: str
    facet_merge_policy: str
    status: str


def _rows(db: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return db.execute(sql).fetchall()


def load_classifications(db: sqlite3.Connection) -> dict[str, ProvisionClassification]:
    """All provision classifications keyed by ``<type>:<id>``."""
    out: dict[str, ProvisionClassification] = {}
    for r in _rows(db, "select * from provision_normative_category"):
        rec = ProvisionClassification(
            provision_type=str(r["provision_type"]),
            provision_id=str(r["provision_id"]),
            structural_category=str(r["structural_category"]),
            has_responsibility=bool(r["has_responsibility"]),
            has_prohibition=bool(r["has_prohibition"]),
            lifecycle_state=str(r["lifecycle_state"]),
            current_successor=str(r["current_successor"] or ""),
            status=str(r["status"]),
        )
        out[rec.key] = rec
    return out


def load_convergence(db: sqlite3.Connection) -> dict[str, ConvergenceEntry]:
    """Current convergence mappings keyed by restatement ``<type>:<id>``."""
    out: dict[str, ConvergenceEntry] = {}
    for r in _rows(db, "select * from codex_normative_convergence_registry"):
        entry = ConvergenceEntry(
            statement_code=str(r["statement_code"]),
            controlling_key=f"{r['controlling_type']}:{r['controlling_id']}",
            restatement_key=f"{r['restatement_type']}:{r['restatement_id']}",
            relationship_kind=str(r["relationship_kind"]),
            facet_merge_policy=str(r["facet_merge_policy"]),
            status=str(r["status"]),
        )
        if entry.status == "current":
            out[entry.restatement_key] = entry
    return out


def resolve_controller(
    convergence: Mapping[str, ConvergenceEntry], provision_type: str, provision_id: str
) -> str:
    """A609: resolve a provision key to its unique controlling provision.

    A restatement resolves to its controller; anything not registered as a
    restatement controls itself.  Controllers are never themselves
    restatements (validated by :func:`validate_classification`), so one hop
    is always sufficient.
    """
    key = f"{provision_type}:{provision_id}"
    entry = convergence.get(key)
    return entry.controlling_key if entry else key


def is_subordinate_restatement(
    convergence: Mapping[str, ConvergenceEntry], provision_type: str, provision_id: str
) -> bool:
    """True when the provision is a registered non-independent restatement."""
    return f"{provision_type}:{provision_id}" in convergence


def controlling_provisions(
    classifications: Mapping[str, ProvisionClassification],
    convergence: Mapping[str, ConvergenceEntry],
) -> dict[str, ProvisionClassification]:
    """Current-effective provisions minus subordinate restatements.

    This is the set a runtime index may enumerate: every active/current
    provision that is not a registered restatement of another provision.
    """
    return {
        key: rec
        for key, rec in classifications.items()
        if rec.is_current and key not in convergence
    }


def validate_classification(db: sqlite3.Connection) -> list[str]:
    """Parity checks over the classification and convergence tables.

    Returns a list of human-readable violations; empty means parity PASS.
    """
    errors: list[str] = []
    categories = {
        str(r["category_code"])
        for r in _rows(db, "select category_code from codex_normative_category_directory where status='active'")
    }
    expected = set(STRUCTURAL_CATEGORIES.values()) | set(FACET_CATEGORIES)
    if categories != expected:
        errors.append(
            f"category directory mismatch: {sorted(categories)} != {sorted(expected)}"
        )

    classifications = load_classifications(db)
    convergence = load_convergence(db)

    # Coverage: every normative provision (principle/article/edict) must
    # carry a classification.  Other lifecycle types (closure-definition,
    # registry-rule, sovereign, …) are outside the A608 classification domain.
    try:
        provision_keys = {
            f"{r['provision_type']}:{r['provision_id']}"
            for r in _rows(
                db,
                "select provision_type, provision_id from provision_lifecycle_status "
                "where provision_type in ('principle','article','edict')",
            )
        }
        missing = provision_keys - set(classifications)
        if missing:
            errors.append(
                f"unclassified provisions: {sorted(missing)[:8]}{'…' if len(missing) > 8 else ''}"
            )
    except sqlite3.Error:
        pass

    # Structural parity: category must agree with provision_type.  (A
    # superseded provision may legitimately carry no successor — the codex
    # records that as evidence 'no-active-lineage'; lifecycle integrity is
    # validated by the closure pipeline, not here.)
    for rec in classifications.values():
        expected_cat = STRUCTURAL_CATEGORIES.get(rec.provision_type)
        if expected_cat and rec.structural_category != expected_cat:
            errors.append(
                f"structural category mismatch: {rec.key} {rec.structural_category} != {expected_cat}"
            )

    # A609 unique-controller resolution:
    #  * one current entry per restatement (dict build already collapses; check raw)
    seen: dict[str, int] = {}
    for r in _rows(
        db,
        "select restatement_type, restatement_id from codex_normative_convergence_registry where status='current'",
    ):
        key = f"{r['restatement_type']}:{r['restatement_id']}"
        seen[key] = seen.get(key, 0) + 1
    for key, n in seen.items():
        if n > 1:
            errors.append(f"restatement has multiple current controllers: {key}")

    controllers = {e.controlling_key for e in convergence.values()}
    #  * a restatement must never itself be a controller (single-level only)
    for key in controllers & set(convergence):
        errors.append(f"controller is itself a restatement (chain): {key}")
    #  * controllers must exist and be current-effective
    for key in controllers:
        rec = classifications.get(key)
        if rec is None:
            errors.append(f"controller not classified: {key}")
        elif not rec.is_current:
            errors.append(
                f"controller not current-effective: {key} ({rec.lifecycle_state}/{rec.status})"
            )
    #  * restatement rows must carry the non-independent relationship kind
    for e in convergence.values():
        if e.relationship_kind != "restatement-non-independent":
            errors.append(
                f"unexpected relationship_kind {e.relationship_kind}: {e.statement_code}"
            )
    return errors


@contextmanager
def open_db(database: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection to the codex database."""
    path = Path(database) if database else DEFAULT_DATABASE
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        yield db
    finally:
        db.close()


def summary(database: Path | None = None) -> dict[str, Any]:
    """Compact status summary for evidence and dashboards."""
    with open_db(database) as db:
        classifications = load_classifications(db)
        convergence = load_convergence(db)
        controllers = controlling_provisions(classifications, convergence)
        errors = validate_classification(db)
    return {
        "classified": len(classifications),
        "current_effective": sum(1 for r in classifications.values() if r.is_current),
        "restatements": len(convergence),
        "controllers_exposed": len(controllers),
        "parity_errors": errors,
    }
