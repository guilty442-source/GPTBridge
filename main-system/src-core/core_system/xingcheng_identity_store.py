"""PostgreSQL store for the Xingcheng personality identity.

A592/A604/A621: the assistant's identity store moved to
``xingcheng_assistant_identity`` — each institution owns its own
database lifecycle; this module holds only the 星澄 personality store,
now persisted in the ``gptbridge_xingcheng`` PostgreSQL schema (SQLite
retired; PostgreSQL is the sole structured-data authority).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared_layer.local.pg_adapter import connect as pg_connect

_SCHEMA = "gptbridge_xingcheng"


class XingchengIdentityStore:
    """Owns the 星澄 personality identity database only."""

    def __init__(self, project_root: Path, schema: str = _SCHEMA) -> None:
        self._project_root = Path(project_root)
        self._schema = schema

    def initialize(self) -> None:
        with pg_connect(self._schema) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS personality_identity ("
                "identity_id text PRIMARY KEY CHECK(identity_id = '星澄'), "
                "display_name text NOT NULL CHECK(display_name = '星澄'), "
                "owner_kind text NOT NULL CHECK(owner_kind = 'native-model'))"
            )
            connection.execute(
                "INSERT INTO personality_identity "
                "(identity_id, display_name, owner_kind) VALUES (?, ?, ?) "
                "ON CONFLICT (identity_id) DO NOTHING",
                ("星澄", "星澄", "native-model"),
            )

    def status(self) -> dict[str, Any]:
        return {
            "personality_database": f"{self._schema}.personality_identity",
            "shared_authority": False,
        }


__all__ = ["XingchengIdentityStore"]
