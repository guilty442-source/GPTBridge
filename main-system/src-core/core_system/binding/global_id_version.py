"""Global ID Version Service — A367 Phase 2: GLOBAL_ID_VERSION.

A367: GLOBAL_ID_VERSION (one resource_id and version across all modules).

This service ensures each resource has a single global ID and version
across all modules, maintaining cross-module consistency.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg


@dataclass(frozen=True)
class GlobalIdResult:
    """Result of global ID assignment."""
    resource_id: str
    module_id: str
    global_id: str
    version: int
    action: str  # 'assigned', 'verified', 'updated', 'conflict'
    detail: str = ""


class GlobalIdVersionService:
    """A367 Phase 2: GLOBAL_ID_VERSION - one resource_id and version across all modules.

    Ensures each resource has a single global ID and version across all modules,
    maintaining cross-module consistency and preventing ID conflicts.
    """

    def __init__(self, pg_connection: Any) -> None:
        self.pg = pg_connection
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Ensure the global_id_version table exists."""
        if self.pg is None:
            return
        try:
            cur = self.pg.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gptbridge_rag.global_id_version (
                    global_id TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (module_id, resource_id)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_global_id_module_resource
                ON gptbridge_rag.global_id_version (module_id, resource_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_global_id_content_hash
                ON gptbridge_rag.global_id_version (content_hash)
            """)
            self.pg.commit()
        except Exception:
            pass
        finally:
            cur.close()

    def _generate_global_id(self, module_id: str, resource_id: str, content_hash: str | None) -> str:
        """Generate a deterministic global ID from module, resource, and content."""
        source = f"{module_id}:{resource_id}"
        if content_hash:
            source += f":{content_hash}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]

    async def assign_global_id(
        self,
        module_id: str,
        resource_id: str,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        """Assign or verify global ID and version for a resource (A367 Phase 2).

        Returns GlobalIdResult with the assigned global ID and version.
        """
        # Generate deterministic global ID
        global_id = self._generate_global_id(module_id, resource_id, content_hash)

        # Check if global ID already exists
        existing = self._fetch_existing_global_id(global_id)

        if existing:
            return self._handle_existing_global_id(
                existing, module_id, resource_id, content_hash
            )
        else:
            return self._assign_new_global_id(
                global_id, module_id, resource_id, content_hash
            )

    def _fetch_existing_global_id(self, global_id: str) -> tuple | None:
        """Fetch existing global ID from database."""
        cur = self.pg.cursor()
        try:
            cur.execute(
                """SELECT global_id, version, content_hash
                   FROM gptbridge_rag.global_id_version
                   WHERE global_id = %s""",
                (global_id,),
            )
            return cur.fetchone()
        finally:
            cur.close()

    def _handle_existing_global_id(
        self,
        existing: tuple,
        module_id: str,
        resource_id: str,
        content_hash: str | None,
    ) -> dict[str, Any]:
        """Handle case where global ID already exists."""
        existing_global_id, existing_version, existing_hash = existing
        if existing_hash == content_hash:
            # Already assigned and content matches
            return {
                "global_id": existing_global_id,
                "version": existing_version,
                "resource_id": module_id + ":" + resource_id,
                "action": "verified",
                "detail": "global ID already assigned with matching content",
            }
        else:
            # Content hash mismatch - conflict
            return {
                "global_id": existing_global_id,
                "version": existing_version,
                "resource_id": module_id + ":" + resource_id,
                "action": "conflict",
                "detail": "content hash mismatch for existing global ID",
            }

    def _assign_new_global_id(
        self,
        global_id: str,
        module_id: str,
        resource_id: str,
        content_hash: str | None,
    ) -> dict[str, Any]:
        """Assign a new global ID."""
        cur = self.pg.cursor()
        try:
            cur.execute(
                """INSERT INTO gptbridge_rag.global_id_version
                      (global_id, resource_id, module_id, version, content_hash)
                   VALUES (%s, %s, %s, 1, %s)""",
                (global_id, resource_id, module_id, content_hash),
            )
            self.pg.commit()
            return {
                "global_id": global_id,
                "version": 1,
                "resource_id": module_id + ":" + resource_id,
                "action": "assigned",
                "detail": "new global ID assigned",
            }
        except Exception as exc:
            self.pg.rollback()
            return {
                "global_id": "",
                "version": 0,
                "resource_id": module_id + ":" + resource_id,
                "action": "error",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        finally:
            cur.close()

    def get_global_id(self, module_id: str, resource_id: str) -> str | None:
        """Get the global ID for a resource."""
        cur = self.pg.cursor()
        try:
            cur.execute(
                """SELECT global_id FROM gptbridge_rag.global_id_version
                   WHERE module_id = %s AND resource_id = %s""",
                (module_id, resource_id),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
        finally:
            cur.close()

    def get_version(self, global_id: str) -> int | None:
        """Get the version for a global ID."""
        cur = self.pg.cursor()
        try:
            cur.execute(
                """SELECT version FROM gptbridge_rag.global_id_version
                   WHERE global_id = %s""",
                (global_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
        finally:
            cur.close()

    def increment_version(self, global_id: str) -> int:
        """Increment the version for a global ID."""
        cur = self.pg.cursor()
        try:
            cur.execute(
                """UPDATE gptbridge_rag.global_id_version
                   SET version = version + 1, updated_at = NOW()
                   WHERE global_id = %s
                   RETURNING version""",
                (global_id,),
            )
            row = cur.fetchone()
            self.pg.commit()
            return int(row[0]) if row else 0
        except Exception:
            self.pg.rollback()
            return 0
        finally:
            cur.close()

    def verify_global_consistency(self) -> dict[str, Any]:
        """Verify global ID consistency across all modules."""
        cur = self.pg.cursor()
        try:
            # Check for duplicate resource_ids across modules
            cur.execute("""
                SELECT resource_id, COUNT(DISTINCT module_id) as module_count
                FROM gptbridge_rag.global_id_version
                GROUP BY resource_id
                HAVING COUNT(DISTINCT module_id) > 1
            """)
            duplicates = cur.fetchall()

            # Check for global_id collisions
            cur.execute("""
                SELECT global_id, COUNT(*) as count
                FROM gptbridge_rag.global_id_version
                GROUP BY global_id
                HAVING COUNT(*) > 1
            """)
            collisions = cur.fetchall()

            return {
                "duplicate_resources": len(duplicates),
                "global_id_collisions": len(collisions),
                "details": {
                    "duplicates": [{"resource_id": d[0], "modules": d[1]} for d in duplicates],
                    "collisions": [{"global_id": c[0], "count": c[1]} for c in collisions],
                },
            }
        finally:
            cur.close()


__all__ = ["GlobalIdVersionService"]