"""PostgreSQL staging-schema lifecycle and ``.sql`` artifact codec.

Post-cutover storage contract (sqlite retirement): the codex authority is the
``gptbridge_codex`` PostgreSQL schema.  Staged generations — isolated copies,
amendment candidates, audit fixtures — are PostgreSQL staging schemas named
``gptbridge_codex_stage_<token>`` (per-invocation unique via
``unique_stage_name``); the reviewable/digestable on-disk artifact
is a deterministic ``.sql`` dump (one statement per line, ``E''``-escaped
literals so a line is always a complete statement).

Compatibility entrypoint (source-size split, A185): the implementation
lives in ``codex_postgresql_stage_schema`` (naming + lifecycle + admin
catalog reads), ``codex_postgresql_stage_artifact`` (the ``.sql`` codec)
and ``codex_postgresql_stage_connection`` (``StageConnection`` + the
``open_*`` entries); every public name below re-exports unchanged for the
wide existing caller surface.

Everything here is fail-closed: no DSN, no schema, no mutation of the live
authority (writes only ever land inside ``*_stage_*`` schemas).
"""

from __future__ import annotations

try:
    from .codex_postgresql_stage_artifact import (
        ARTIFACT_HEADER,
        artifact_table_names,
        artifact_version,
        dump_schema,
        materialize_artifact,
    )
    from .codex_postgresql_stage_connection import (
        StageConnection,
        open_artifact,
        open_codex_store,
        open_stage,
    )
    from .codex_postgresql_stage_schema import (
        STALE_STAGE_TTL_SECONDS,
        STAGE_PREFIX,
        clone_authority,
        clone_schema,
        create_empty_schema,
        drop_schema,
        schema_exists,
        stage_table_names,
        sweep_stale_stage_schemas,
        unique_stage_name,
    )
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_stage_artifact import (
        ARTIFACT_HEADER,
        artifact_table_names,
        artifact_version,
        dump_schema,
        materialize_artifact,
    )
    from codex_postgresql_stage_connection import (
        StageConnection,
        open_artifact,
        open_codex_store,
        open_stage,
    )
    from codex_postgresql_stage_schema import (
        STALE_STAGE_TTL_SECONDS,
        STAGE_PREFIX,
        clone_authority,
        clone_schema,
        create_empty_schema,
        drop_schema,
        schema_exists,
        stage_table_names,
        sweep_stale_stage_schemas,
        unique_stage_name,
    )

__all__ = [
    "ARTIFACT_HEADER",
    "STAGE_PREFIX",
    "StageConnection",
    "artifact_table_names",
    "artifact_version",
    "clone_authority",
    "clone_schema",
    "create_empty_schema",
    "drop_schema",
    "dump_schema",
    "materialize_artifact",
    "open_artifact",
    "open_codex_store",
    "open_stage",
    "schema_exists",
    "stage_table_names",
    "sweep_stale_stage_schemas",
    "unique_stage_name",
]
