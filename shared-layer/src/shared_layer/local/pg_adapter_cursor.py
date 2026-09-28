"""Cursor facade for ``pg_adapter`` (source-size split)."""
from __future__ import annotations

from typing import Any, Iterable, Iterator, Sequence

from .pg_adapter_translate import translate


class PgCursor:
    """Minimal cursor surface: fetchone/fetchall/fetchmany/rowcount.

    Rows behave like ``sqlite3.Row``: index, name and ``keys()`` access,
    plus value-order unpacking, via :class:`PgRow`.
    """

    def __init__(self, cursor: Any, pk_resolver: Any = None) -> None:
        self._cursor = cursor
        self._pk_resolver = pk_resolver

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cursor)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def fetchmany(self, size: int) -> list[Any]:
        return self._cursor.fetchmany(size)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> Any:
        return getattr(self._cursor, "lastrowid", None)

    def close(self) -> None:
        self._cursor.close()

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> "PgCursor":
        translated = translate(statement, pk_resolver=self._pk_resolver)
        if not translated:
            return self
        self._cursor.execute(translated, parameters)
        return self

    def executemany(self, statement: str, seq: Iterable[Sequence[Any]]) -> "PgCursor":
        translated = translate(statement, pk_resolver=self._pk_resolver)
        if not translated:
            return self
        self._cursor.executemany(translated, list(seq))
        return self
