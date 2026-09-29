"""PostgreSQL authority and one-way ``.sql`` artifact import for the Codex.

Compatibility entrypoint (source-size split): the implementation lives in
``codex_postgresql_dsn`` / ``codex_postgresql_pool`` /
``codex_postgresql_stage`` / ``codex_postgresql_import`` /
``codex_postgresql_export``; every public name below re-exports unchanged
for the wide existing caller surface.
"""

from __future__ import annotations

try:
    from .codex_postgresql_dsn import (
        _IDENTIFIER,
        CODEX_AUTHORITY_URI,
        CODEX_SCHEMA,
        admin_dsn,
        runtime_dsn,
    )
    from .codex_postgresql_export import (
        export_postgresql_codex,
        verify_sql_parity,
    )
    from .codex_postgresql_import import (
        _version_regresses,
        _version_units,
        import_codex_artifact,
    )
    from .codex_postgresql_pool import (
        authority_state,
        close_cached_connections,
        readonly_connection,
    )
    from .codex_postgresql_stage import (
        StageConnection,
        artifact_table_names,
        artifact_version,
        clone_authority,
        clone_schema,
        drop_schema,
        dump_schema,
        materialize_artifact,
        open_artifact,
        open_codex_store,
        open_stage,
        schema_exists,
    )
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import (
        _IDENTIFIER,
        CODEX_AUTHORITY_URI,
        CODEX_SCHEMA,
        admin_dsn,
        runtime_dsn,
    )
    from codex_postgresql_export import (
        export_postgresql_codex,
        verify_sql_parity,
    )
    from codex_postgresql_import import (
        _version_regresses,
        _version_units,
        import_codex_artifact,
    )
    from codex_postgresql_pool import (
        authority_state,
        close_cached_connections,
        readonly_connection,
    )
    from codex_postgresql_stage import (
        StageConnection,
        artifact_table_names,
        artifact_version,
        clone_authority,
        clone_schema,
        drop_schema,
        dump_schema,
        materialize_artifact,
        open_artifact,
        open_codex_store,
        open_stage,
        schema_exists,
    )

__all__ = [
    "CODEX_AUTHORITY_URI",
    "CODEX_SCHEMA",
    "StageConnection",
    "admin_dsn",
    "artifact_table_names",
    "artifact_version",
    "authority_state",
    "clone_authority",
    "clone_schema",
    "close_cached_connections",
    "drop_schema",
    "dump_schema",
    "export_postgresql_codex",
    "import_codex_artifact",
    "materialize_artifact",
    "open_artifact",
    "open_codex_store",
    "open_stage",
    "readonly_connection",
    "runtime_dsn",
    "schema_exists",
    "verify_sql_parity",
]
