from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row


class ModuleLocatorRepository:
    """Owning-module-only PostgreSQL locator map; never stored in the central index."""

    def __init__(self, dsn: str, module_id: str) -> None:
        self._dsn = str(dsn or "").strip()
        self._module_id = str(module_id or "").strip().casefold()
        if not self._dsn or not self._module_id:
            raise ValueError("MODULE_LOCATOR_CONFIGURATION_REQUIRED")

    def resolve(self, locator_id: uuid.UUID, resource_id: str) -> str | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            row: dict[str, Any] | None = connection.execute(
                """
                SELECT ntfs_relative_path FROM module_data.locator_map
                WHERE locator_id=%s AND resource_id=%s AND module_id=%s
                """,
                (locator_id, resource_id, self._module_id),
            ).fetchone()
        return None if not row else str(row["ntfs_relative_path"])


__all__ = ["ModuleLocatorRepository"]
