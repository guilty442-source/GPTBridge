from __future__ import annotations

"""provision_local_architecture — codex-native local sqlite provisioning.

Replaces the retired ``provision_postgresql_architecture.py`` (PostgreSQL
provisioning).  Per Governance Codex A44/E30 the SQL engine is local sqlite3,
provisioned here with the central-index schema, the transport store schema and
the module locator map.  No PostgreSQL/psycopg, no external service.
"""

import json
import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _module_ids() -> tuple[str, ...]:
    modules: list[str] = []
    for manifest in ROOT.glob("*/manifest.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        module_id = str(payload.get("id") or "").strip().casefold()
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", module_id):
            modules.append(module_id)
    return tuple(sorted(set(modules)))


def _ensure_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _provision_central_index() -> int:
    db = sqlite3.connect(_ensure_path(ROOT / "shared-layer" / "runtime" / "central-index.sqlite3"))
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS resource (
                resource_id TEXT NOT NULL PRIMARY KEY,
                platform_id TEXT NOT NULL,
                module_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                data_category TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_label TEXT NOT NULL,
                logical_key TEXT NOT NULL,
                classification TEXT NOT NULL,
                locator_id TEXT NOT NULL,
                content_hash TEXT,
                metadata TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS locations (
                locator_id TEXT NOT NULL PRIMARY KEY,
                resource_id TEXT NOT NULL UNIQUE,
                module_id TEXT NOT NULL,
                executor_type TEXT NOT NULL,
                location_key TEXT NOT NULL,
                physical_location TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_locations_resolve
                ON locations (resource_id, executor_type, status);
            """
        )
    finally:
        db.close()
    return 1


def _provision_transport() -> int:
    for name in ("system", "ai"):
        db = sqlite3.connect(_ensure_path(ROOT / "shared-layer" / "runtime" / f"{name}.sqlite3"))
        try:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_request (
                    channel_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    requester_actor TEXT NOT NULL,
                    target_tool_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    response TEXT,
                    progress TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tool_request_claim
                    ON tool_request (target_tool_id, status, created_at, request_id);
                """
            )
        finally:
            db.close()
    return 2


def _module_locator_path(module_id: str) -> Path:
    return _ensure_path(ROOT / "shared-layer" / "runtime" / "module-locators" / f"{module_id}.sqlite3")


def _provision_module_locators(modules: tuple[str, ...]) -> int:
    for module_id in modules:
        db = sqlite3.connect(_module_locator_path(module_id))
        try:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS module_locator_map (
                    locator_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    ntfs_relative_path TEXT NOT NULL,
                    PRIMARY KEY (locator_id, resource_id, module_id)
                );
                """
            )
        finally:
            db.close()
    return len(modules)


def main() -> int:
    modules = _module_ids()
    schema_count = _provision_central_index()
    transport_count = _provision_transport()
    locator_count = _provision_module_locators(modules)
    print(json.dumps({
        "ok": True,
        "engine": "local-sqlite3",
        "modules": modules,
        "central_schema_tables": schema_count,
        "transport_databases": transport_count,
        "module_locator_databases": locator_count,
        "deprecated_postgresql": True,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())