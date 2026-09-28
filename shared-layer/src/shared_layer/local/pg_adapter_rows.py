"""Row types for ``pg_adapter`` (source-size split)."""
from __future__ import annotations

from typing import Any, Iterator, Sequence


class PgRow(dict):
    """``sqlite3.Row``-compatible row: name access, positional access and
    value-order unpacking (``for a, b in row`` yields values, not keys).

    ``dict(row)`` still produces ``{column: value}`` because the row *is* a
    dict; the positional view is materialised once at construction.
    """

    __slots__ = ("_values",)

    def __init__(
        self, values: dict[str, Any], ordered: Sequence[Any] | None = None
    ) -> None:
        super().__init__(values)
        # Unnamed expressions share a column name (e.g. several COALESCE(...)
        # columns all named "coalesce"); the dict collapses duplicates, so the
        # positional view must come from the raw value sequence.
        self._values = list(ordered) if ordered is not None else list(values.values())

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        return super().__getitem__(key)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)


def _pg_row_factory(cursor: Any) -> Any:
    description = cursor.description
    if description is None:
        # Statements without a result set (DDL, DML without RETURNING) still
        # ask for a factory — return raw tuples for the (empty) results.
        return tuple
    names = [column.name for column in description]

    def make_row(values: Sequence[Any]) -> PgRow:
        return PgRow(dict(zip(names, values)), ordered=values)

    return make_row
