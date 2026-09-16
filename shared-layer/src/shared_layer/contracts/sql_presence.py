"""SQL presence contract — row-not-found and NULL are different states.

A query that returns zero rows is NOT the same as a row whose column is
NULL.  ``SqlPresence`` forces callers to distinguish:

    ROW_MISSING   — no row satisfied the predicate
    VALUE_NULL    — row exists; the column is NULL
    VALUE         — row exists; column holds a value

``read_cell`` unwraps a fetched row under the contract; ``one`` converts
a row-count fetch into ROW_MISSING / VALUE.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Sequence, Tuple


class SqlPresence(Enum):
    ROW_MISSING = "row_missing"
    VALUE_NULL = "value_null"
    VALUE = "value"


def read_cell(row: Optional[Sequence[Any]], column: int = 0) -> Tuple[SqlPresence, Any]:
    """Classify a single-column fetch result.

    ``row`` is the raw fetchone() result: None means no row; otherwise
    inspect the requested column for NULL.
    """
    if row is None:
        return SqlPresence.ROW_MISSING, None
    cell = row[column]
    if cell is None:
        return SqlPresence.VALUE_NULL, None
    return SqlPresence.VALUE, cell


def row_presence(row: Optional[Sequence[Any]]) -> SqlPresence:
    """Presence of the row itself (multi-column fetches)."""
    return SqlPresence.ROW_MISSING if row is None else SqlPresence.VALUE


__all__ = ["SqlPresence", "read_cell", "row_presence"]
