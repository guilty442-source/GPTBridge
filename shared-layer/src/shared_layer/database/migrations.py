from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection


@dataclass(frozen=True)
class MigrationResult:
    applied: tuple[str, ...]


class MigrationRunner:
    """Checksum-locked, Python-driven SQL migrations."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def run(self, connection: Connection) -> MigrationResult:
        connection.execute("CREATE SCHEMA IF NOT EXISTS gptbridge_migration")
        connection.execute("""CREATE TABLE IF NOT EXISTS gptbridge_migration.history (
            migration_id text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        applied: list[str] = []
        for path in sorted(self.directory.glob("*.sql")):
            body = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
            row = connection.execute(
                "SELECT checksum FROM gptbridge_migration.history WHERE migration_id=%s", (path.name,)
            ).fetchone()
            if row:
                existing = row[0] if not isinstance(row, dict) else row["checksum"]
                if existing != checksum:
                    raise RuntimeError(f"MIGRATION_CHECKSUM_MISMATCH:{path.name}")
                continue
            connection.execute(body)
            connection.execute(
                "INSERT INTO gptbridge_migration.history(migration_id,checksum) VALUES (%s,%s)",
                (path.name, checksum),
            )
            applied.append(path.name)
        return MigrationResult(tuple(applied))


__all__ = ["MigrationResult", "MigrationRunner"]
