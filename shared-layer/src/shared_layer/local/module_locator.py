"""module_locator — codex-native owning-module locator map on PostgreSQL.

Owning-module-only contract (A219/E21 + A37/E23), never stored in the
central index.  A610/A621: the SQLite file was retired; the map lives in
the module's PostgreSQL schema via ``shared_layer.local.pg_adapter``.
"""

from __future__ import annotations

import threading
import uuid
from typing import Final

from . import pg_adapter

_SCHEMA: Final = (
    """CREATE TABLE IF NOT EXISTS module_locator_map (
        locator_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        module_id TEXT NOT NULL,
        ntfs_relative_path TEXT NOT NULL,
        PRIMARY KEY (locator_id, resource_id, module_id)
    )"""
)


class LocalModuleLocatorRepository:
    """Owning-module-only PostgreSQL locator map.

    ``schema`` names the module-private PostgreSQL schema that owns the
    map (e.g. ``gptbridge_xingcheng_main``).
    """

    def __init__(self, schema: str, module_id: str) -> None:
        self._schema = str(schema or "").strip()
        if not self._schema:
            raise ValueError("MODULE_LOCATOR_CONFIGURATION_REQUIRED")
        self._module_id = str(module_id or "").strip().casefold()
        if not self._module_id:
            raise ValueError("MODULE_LOCATOR_CONFIGURATION_REQUIRED")
        self._lock = threading.RLock()
        self._connection: pg_adapter.PgConnection | None = None
        with self._lock:
            self._connect().execute(_SCHEMA)
            self._connection.commit()

    def _connect(self) -> pg_adapter.PgConnection:
        if self._connection is None:
            self._connection = pg_adapter.connect(
                self._schema, autocommit=False
            )
        return self._connection

    def put(self, locator_id: uuid.UUID, resource_id: str, ntfs_relative_path: str) -> None:
        with self._lock:
            connection = self._connect()
            connection.execute(
                "INSERT OR REPLACE INTO module_locator_map (locator_id,resource_id,module_id,ntfs_relative_path) VALUES (?,?,?,?)",
                (str(locator_id), resource_id, self._module_id, ntfs_relative_path),
            )
            connection.commit()

    def resolve(self, locator_id: uuid.UUID, resource_id: str) -> str | None:
        with self._lock:
            row = self._connect().execute(
                "SELECT ntfs_relative_path FROM module_locator_map WHERE locator_id=? AND resource_id=? AND module_id=?",
                (str(locator_id), resource_id, self._module_id),
            ).fetchone()
        return None if row is None else str(row["ntfs_relative_path"])

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


ModuleLocatorRepository = LocalModuleLocatorRepository

__all__ = ["LocalModuleLocatorRepository", "ModuleLocatorRepository"]
