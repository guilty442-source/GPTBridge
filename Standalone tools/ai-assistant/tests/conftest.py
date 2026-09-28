from __future__ import annotations

import sys
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SERVICES_ROOT = TOOL_ROOT / "src" / "backend" / "services"
SRC_ROOT = TOOL_ROOT / "src"
SHARED_SRC = WORKSPACE_ROOT / "shared-layer" / "src"

for path in (SERVICES_ROOT, SRC_ROOT, SHARED_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest


@pytest.fixture(autouse=True)
def offline_ollama_probe(monkeypatch: pytest.MonkeyPatch):
    """A57: tests never spawn the real ``ollama`` CLI — the probe hangs when
    the binary exists but its daemon is down. Pin it to not-ready."""
    from ai_nexus.integration.star_channel import InvestmentAiConnections

    monkeypatch.setattr(
        InvestmentAiConnections, "_ollama_ready", staticmethod(lambda: False)
    )


@pytest.fixture(autouse=True)
def isolated_pg_schema(monkeypatch: pytest.MonkeyPatch):
    """A621: TradingStore persists to PostgreSQL — each test gets a throwaway
    schema instead of touching live ``gptbridge_ai_nexus``."""
    import uuid

    schema = "nexus_test_" + uuid.uuid4().hex[:12]
    import psycopg

    from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn

    dsn = resolve_dsn(DsnPurpose.ADMIN).dsn
    with psycopg.connect(dsn, connect_timeout=5) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        c.execute(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO gptbridge_runtime')
        c.commit()
    monkeypatch.setenv("AI_NEXUS_PG_SCHEMA", schema)
    monkeypatch.setenv("PG_ADAPTER_POOL", "0")
    try:
        yield schema
    finally:
        try:
            from shared_layer.local import pg_adapter

            pg_adapter.close_pool()
        except Exception:
            pass
        try:
            with psycopg.connect(dsn, connect_timeout=5) as c:
                # Stores stay checked out after the test ends; DROP SCHEMA
                # would block on their locks — terminate only backends
                # holding locks inside OUR throwaway schema.
                c.execute("SET lock_timeout = '10s'")
                c.execute(
                    "SELECT pg_terminate_backend(l.pid) FROM pg_locks l "
                    "WHERE l.pid <> pg_backend_pid() AND ("
                    "  (l.locktype = 'relation' AND l.relation IN ("
                    "    SELECT c2.oid FROM pg_class c2 "
                    "    JOIN pg_namespace n ON c2.relnamespace = n.oid "
                    "    WHERE n.nspname = %s))"
                    "  OR (l.locktype = 'object' AND l.classid = 'pg_namespace'::regclass"
                    "      AND l.objid = (SELECT oid FROM pg_namespace WHERE nspname = %s))"
                    ")",
                    (schema, schema),
                )
                c.execute(f'DROP SCHEMA "{schema}" CASCADE')
                c.commit()
        except Exception:
            pass
