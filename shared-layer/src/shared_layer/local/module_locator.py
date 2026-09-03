"""module_locator — codex-native owning-module locator map on sqlite3.

Stdlib-only replacement for ``shared_layer.module_locator_repository
.ModuleLocatorRepository`` (A35/E21 + A37/E23).  Same contract: owning-module
only, never stored in the central index.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Final

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
    """Owning-module-only local sqlite locator map."""

    def __init__(self, db_path: Path | str, module_id: str) -> None:
        self._db_path = Path(db_path).resolve()
        self._module_id = str(module_id or "").strip().casefold()
        if not self._module_id:
            raise ValueError("MODULE_LOCATOR_CONFIGURATION_REQUIRED")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def put(self, locator_id: uuid.UUID, resource_id: str, ntfs_relative_path: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO module_locator_map (locator_id,resource_id,module_id,ntfs_relative_path) VALUES (?,?,?,?)",
                (str(locator_id), resource_id, self._module_id, ntfs_relative_path),
            )

    def resolve(self, locator_id: uuid.UUID, resource_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT ntfs_relative_path FROM module_locator_map WHERE locator_id=? AND resource_id=? AND module_id=?",
                (str(locator_id), resource_id, self._module_id),
            ).fetchone()
        return None if row is None else str(row["ntfs_relative_path"])


ModuleLocatorRepository = LocalModuleLocatorRepository

__all__ = ["LocalModuleLocatorRepository", "ModuleLocatorRepository"]