"""PostgreSQL authority and one-way SQLite predecessor import for the Codex.

Compatibility entrypoint (source-size split): the implementation lives in
``codex_postgresql_dsn`` / ``codex_postgresql_pool`` /
``codex_postgresql_import`` / ``codex_postgresql_export``; every public
name below re-exports unchanged for the wide existing caller surface.
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
        verify_sqlite_parity,
    )
    from .codex_postgresql_import import (
        _version_regresses,
        _version_units,
        import_sqlite_predecessor,
    )
    from .codex_postgresql_pool import (
        authority_state,
        close_cached_connections,
        readonly_connection,
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
        verify_sqlite_parity,
    )
    from codex_postgresql_import import (
        _version_regresses,
        _version_units,
        import_sqlite_predecessor,
    )
    from codex_postgresql_pool import (
        authority_state,
        close_cached_connections,
        readonly_connection,
    )

__all__ = [
    "CODEX_AUTHORITY_URI",
    "CODEX_SCHEMA",
    "admin_dsn",
    "authority_state",
    "close_cached_connections",
    "export_postgresql_codex",
    "import_sqlite_predecessor",
    "readonly_connection",
    "runtime_dsn",
    "verify_sqlite_parity",
]
