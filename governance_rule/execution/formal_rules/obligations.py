"""Implementation obligation tracker (A284/A288/A289/A290/A293/A294/A295/A296/A343/A354).

法典依據:
- A292 mandatory-implementation-obligation-special-law: obligations follow
  the lifecycle ``mandated > planned > in-progress > evidence-submitted >
  verified > complete`` (terminal ``failed/escalated/superseded``); evidence
  acceptance and gate decisions are owned by the responsible sovereign.
- A288/A289/A290/A293/A294/A295/A296/A343/A354 declare the ten mandated
  target states; ``implementation_obligations`` registry rows carry target
  state, due date, acceptance evidence, noncompliance effect and waiver
  rule.
- A435 BOUNDED_MACHINE_LOOKUP: obligation rows are read from the official
  codex through the governed read-only repository connection.  This module
  is the read-only projector: it computes lifecycle status, due-date state
  and acceptance-evidence readiness from the registry — it never fabricates
  completion (A288 FORBID:completion-without-evidence).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from governance_rule.execution.codex_repository import (
    CODEX_DATABASE_PATH,
    codex_readonly_connection,
)

IMPLEMENTATION_OBLIGATIONS_TABLE = "implementation_obligations"

# A292 lifecycle (ordered main spine + terminal states).
LIFECYCLE_MAIN = (
    "mandated",
    "planned",
    "in-progress",
    "evidence-submitted",
    "verified",
    "complete",
)
LIFECYCLE_TERMINAL = ("failed", "escalated", "superseded")

# Forward transitions allowed by A292 (null = state already terminal/complete).
_ALLOWED_TRANSITIONS = {
    "mandated": "planned",
    "planned": "in-progress",
    "in-progress": "evidence-submitted",
    "evidence-submitted": "verified",
    "verified": "complete",
    "complete": None,
    "failed": None,
    "escalated": "superseded",
    "superseded": None,
}


@dataclass(frozen=True)
class ImplementationObligation:
    """One row of ``implementation_obligations`` (A292 registry contract)."""

    obligation_code: str
    declaration_provision: str
    implementation_owner: str
    target_state: str
    current_state: str
    deadline_rule: str
    acceptance_evidence: str
    noncompliance_effect: str
    waiver_rule: str
    introduced_version: str
    created_at_utc: str
    due_at_utc: str
    previous_state: str
    verification_owner: str
    escalation_rule: str

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> "ImplementationObligation":
        return cls(
            obligation_code=str(row.get("obligation_code", "")),
            declaration_provision=str(row.get("declaration_provision", "")),
            implementation_owner=str(row.get("implementation_owner", "")),
            target_state=str(row.get("target_state", "")),
            current_state=str(row.get("current_state", "")),
            deadline_rule=str(row.get("deadline_rule", "")),
            acceptance_evidence=str(row.get("acceptance_evidence", "")),
            noncompliance_effect=str(row.get("noncompliance_effect", "")),
            waiver_rule=str(row.get("waiver_rule", "")),
            introduced_version=str(row.get("introduced_version", "")),
            created_at_utc=str(row.get("created_at_utc", "")),
            due_at_utc=str(row.get("due_at_utc", "")),
            previous_state=str(row.get("previous_state", "")),
            verification_owner=str(row.get("verification_owner", "")),
            escalation_rule=str(row.get("escalation_rule", "")),
        )

    @property
    def due_at(self) -> datetime | None:
        return parse_utc(self.due_at_utc)

    def is_terminal(self) -> bool:
        return self.current_state in LIFECYCLE_TERMINAL or self.current_state == "complete"

    def is_overdue(self, now: datetime | None = None) -> bool:
        if self.is_terminal():
            return False
        due = self.due_at
        if due is None:
            return False
        return (now or datetime.now(timezone.utc)) > due

    def next_transition(self) -> str | None:
        return _ALLOWED_TRANSITIONS.get(self.current_state)

    def can_transition_to(self, target: str) -> tuple[bool, str]:
        allowed = _ALLOWED_TRANSITIONS.get(self.current_state)
        if target == self.current_state:
            return False, f"{target} equals current state {self.current_state}"
        if allowed is None:
            return False, f"state {self.current_state} is terminal or complete"
        if target != allowed:
            return False, f"allowed transition from {self.current_state} is only {allowed}, not {target}"
        return True, "ok"


def parse_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp; None for empty/malformed."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def _obligation_rows(
    connection: sqlite3.Connection,
) -> tuple[dict[str, str], ...]:
    columns = [
        column[1] for column in connection.execute("PRAGMA table_info(implementation_obligations)")
    ]
    return tuple(
        dict(zip(columns, row))
        for row in connection.execute("SELECT * FROM implementation_obligations")  # sql-ok: PRAGMA-driven all-columns contract snapshot
    )


def load_obligations(
    database: Path = CODEX_DATABASE_PATH,
) -> tuple[ImplementationObligation, ...]:
    """Read every implementation obligation from the official codex SQLite.

    Reads through the governed read-only repository connection (A279/A435);
    degrades to an empty tuple when the codex is unreadable (callers treat
    a missing obligation as an audit finding, never as complete).
    """
    try:
        with codex_readonly_connection(database) as connection:
            rows = _obligation_rows(connection)
        return tuple(ImplementationObligation.from_row(row) for row in rows)
    except (OSError, sqlite3.Error, ValueError, KeyError, RuntimeError):
        return ()


def obligation(
    obligation_code: str,
    obligations: Iterable[ImplementationObligation] | None = None,
) -> ImplementationObligation | None:
    obligations = obligations if obligations is not None else load_obligations()
    code = str(obligation_code or "").strip()
    for item in obligations:
        if item.obligation_code == code:
            return item
    return None


def overdue_obligations(
    obligations: Iterable[ImplementationObligation] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[ImplementationObligation, ...]:
    """Obligations past their due date that have not reached a terminal/complete state."""
    return tuple(
        item
        for item in (obligations if obligations is not None else load_obligations())
        if item.is_overdue(now)
    )


def lifecycle_status(
    obligations: Iterable[ImplementationObligation] | None = None,
) -> dict[str, int]:
    """Count of obligations per A292 lifecycle state (read-only projection)."""
    counts: dict[str, int] = {}
    for item in obligations if obligations is not None else load_obligations():
        counts.setdefault(item.current_state, 0)
        counts[item.current_state] += 1
    return counts


def acceptance_criteria(obligation_code: str) -> tuple[str, ...]:
    """Normalise the acceptance-evidence field into individual criteria."""
    item = obligation(obligation_code)
    if item is None:
        return ()
    raw = str(item.acceptance_evidence or "")
    return tuple(
        part.strip()
        for part in re.split(r"[+|;,]", raw)
        if part.strip()
    )


def evidence_readiness(
    provided: Iterable[str],
    obligations: Iterable[ImplementationObligation] | None = None,
    *,
    obligation_code: str,
) -> tuple[bool, tuple[str, ...]]:
    """Check supplied evidence markers against one obligation's acceptance criteria.

    Returns (ready, missing).  Missing criteria always keep ``ready`` False —
    completion without evidence is forbidden (A288 FORBID:completion-without-evidence).
    """
    criteria = acceptance_criteria(obligation_code)
    if not criteria:
        return False, ("no acceptance evidence declared",)
    have = {str(marker).strip() for marker in provided}
    missing = tuple(c for c in criteria if c not in have)
    return (not missing, missing)


__all__ = [
    "IMPLEMENTATION_OBLIGATIONS_TABLE",
    "LIFECYCLE_MAIN",
    "LIFECYCLE_TERMINAL",
    "ImplementationObligation",
    "acceptance_criteria",
    "evidence_readiness",
    "lifecycle_status",
    "load_obligations",
    "obligation",
    "overdue_obligations",
    "parse_utc",
]