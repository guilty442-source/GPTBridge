from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection


@contextmanager
def transaction(connection: Connection) -> Iterator[Connection]:
    with connection.transaction():
        yield connection


@contextmanager
def governed_transaction(connection: Connection, *, actor_kind: str) -> Iterator[Connection]:
    normalized = str(actor_kind or "").strip().casefold()
    if normalized not in {"module", "xingcheng", "system"}:
        raise ValueError("INVALID_DATABASE_ACTOR_KIND")
    with connection.transaction():
        connection.execute(
            "SELECT set_config('gptbridge.actor_kind', %s, true)", (normalized,)
        )
        yield connection


__all__ = ["governed_transaction", "transaction"]
