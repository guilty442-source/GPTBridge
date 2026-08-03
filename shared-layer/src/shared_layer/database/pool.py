from __future__ import annotations

from .connection import ConnectionManager


class PostgreSQLPool(ConnectionManager):
    """Named shared-layer pool; connections still execute against real PostgreSQL."""


__all__ = ["PostgreSQLPool"]
