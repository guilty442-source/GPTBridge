"""Shared pytest fixtures for the ai-collaboration tool.

Restores the retired sqlite ``tmp_path`` isolation: each test gets a
throwaway PostgreSQL schema instead of mutating the real
``gptbridge_collab`` schema (A610/A621 — PG is the sole data authority).

Schemas are provisioned through the governed ADMIN-purpose DSN and owned
by the runtime role, then dropped on teardown.  If the admin DSN is not
resolvable the fixture fails closed — tests must never silently fall back
to the shared production schema.
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "shared-layer" / "src"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from shared_layer.local import pg_adapter
from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn

_SCHEMA_COUNTER = itertools.count(1)


@pytest.fixture(autouse=True)
def collab_isolated_pg_schema(monkeypatch: pytest.MonkeyPatch):
    admin_binding = resolve_dsn(DsnPurpose.ADMIN)
    runtime_binding = resolve_dsn(DsnPurpose.RUNTIME)
    schema = f"gptbridge_collab_t{os.getpid()}_{next(_SCHEMA_COUNTER)}"
    admin = pg_adapter.connect("public", dsn=admin_binding.dsn)
    try:
        admin.execute(
            f'CREATE SCHEMA "{schema}" AUTHORIZATION "{runtime_binding.user}"'
        )
        monkeypatch.setenv("AI_COLLAB_PG_SCHEMA", schema)
        yield
    finally:
        try:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        except Exception:
            pass
        admin.close()
