"""sqlite3.Connection-compatible facade for ``pg_adapter``
(source-size split)."""
from __future__ import annotations

from functools import partial
from typing import Any, Iterable, Sequence

from .pg_adapter_cursor import PgCursor
from .pg_adapter_pool import _pool_enabled, _pool_release
from .pg_adapter_translate import _resolve_pk_cached, _split_script


class PgConnection:
    """sqlite3.Connection-compatible facade over a psycopg connection."""

    def __init__(
        self,
        connection: Any,
        schema: str,
        pool_key: tuple[Any, ...] | None = None,
    ) -> None:
        self._connection = connection
        self.schema = schema
        self.row_factory: Any = None  # accepted for API parity; rows are dicts
        self._pk_cache: dict[str, list[str] | None] = {}
        self._pool_key = pool_key

    # -- sqlite3 surface ------------------------------------------------------

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> PgCursor:
        cur = self.cursor()
        return cur.execute(statement, parameters)

    def executemany(self, statement: str, seq: Iterable[Sequence[Any]]) -> PgCursor:
        cur = self.cursor()
        return cur.executemany(statement, seq)

    def executescript(self, script: str) -> PgCursor:
        cur = self.cursor()
        for statement in _split_script(script):
            cur.execute(statement)  # sql-ok: executescript parity — bounded split of caller-owned DDL script
        return cur

    def cursor(self) -> PgCursor:
        return PgCursor(
            self._connection.cursor(),
            pk_resolver=partial(_resolve_pk_cached, self._pk_cache, self._connection),
        )

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        if self._pool_key is not None and _pool_enabled():
            _pool_release(self._pool_key, connection)
        else:
            connection.close()

    @property
    def total_changes(self) -> int:
        return 0  # informational only in sqlite; not required by callers

    # -- context manager: sqlite3 semantics = commit on success ---------------

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._connection is None:
            return False
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        self.close()  # return the backend to the pool on block exit
        return False
