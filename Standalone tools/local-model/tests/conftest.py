from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

for path in (
    ROOT / "local-model" / "src" / "backend" / "services",
    ROOT / "shared-layer" / "src",
    ROOT,
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def _isolate_native_engine_settings(monkeypatch, tmp_path_factory):
    """測試一律不讀機器上的執行期 native-engine 設定（可決定性）。"""
    try:
        from xingcheng.infrastructure import native_engine as module
    except Exception:  # pragma: no cover - 匯入失敗時不影響其他測試
        return
    root = tmp_path_factory.mktemp("native-engine-isolation")
    if hasattr(module, "settings_path"):
        monkeypatch.setattr(module, "settings_path", lambda: root / "absent.json")
    if hasattr(module, "_engine_cache"):
        monkeypatch.setattr(module, "_engine_cache", {})
    if hasattr(module, "NATIVE_EXECUTION_LEDGER"):
        monkeypatch.setattr(
            module, "NATIVE_EXECUTION_LEDGER", str(root / "ledger.jsonl")
        )
    for name in ("NATIVE_ENGINE_ENV", "NATIVE_CHECKPOINT_ENV"):
        env_name = getattr(module, name, "")
        if env_name:
            monkeypatch.delenv(env_name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_vector_store_schema(monkeypatch):
    """A621: LocalVectorStore persists to PostgreSQL — tests get a throwaway
    schema instead of touching live ``gptbridge_rag`` (whose persisted
    collection_state dimension would mismatch and contaminate)."""
    import uuid

    schema = "vs_test_" + uuid.uuid4().hex[:12]
    try:
        import psycopg

        from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn

        dsn = resolve_dsn(DsnPurpose.ADMIN).dsn
        with psycopg.connect(dsn, connect_timeout=5) as c:
            c.execute(f'CREATE SCHEMA "{schema}"')
            c.execute(
                f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO gptbridge_runtime'
            )
            c.commit()
    except Exception:
        yield ""
        return
    monkeypatch.setenv("LOCAL_VECTOR_STORE_PG_SCHEMA", schema)
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


_XINGCHENG_SCOPES = ("main", "investment", "mathematical", "coding")


def _terminate_schema_locks(dsn: str, schema: str) -> None:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as c:
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


@pytest.fixture(autouse=True)
def _isolate_xingcheng_repo_schemas(monkeypatch):
    """A621: LocalAiRepository writes per-scope schemas — tests get throwaway
    schemas per scope so live ``gptbridge_xingcheng_*`` tables (legacy DDL,
    accumulated rows) never leak into assertions."""
    import uuid

    suffix = uuid.uuid4().hex[:10]
    schemas = {s: f"xc_test_{s}_{suffix}" for s in _XINGCHENG_SCOPES}
    schemas["shared"] = f"xc_test_shared_{suffix}"
    try:
        import psycopg

        from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn

        dsn = resolve_dsn(DsnPurpose.ADMIN).dsn
        with psycopg.connect(dsn, connect_timeout=5) as c:
            for name in schemas.values():
                c.execute(f'CREATE SCHEMA "{name}"')
                c.execute(
                    f'GRANT USAGE, CREATE ON SCHEMA "{name}" TO gptbridge_runtime'
                )
            c.commit()
    except Exception:
        yield {}
        return
    for scope, name in schemas.items():
        monkeypatch.setenv(f"XINGCHENG_{scope.upper()}_PG_SCHEMA", name)
    try:
        yield schemas
    finally:
        try:
            from shared_layer.local import pg_adapter

            pg_adapter.close_pool()
        except Exception:
            pass
        for name in schemas.values():
            try:
                _terminate_schema_locks(dsn, name)
            except Exception:
                pass