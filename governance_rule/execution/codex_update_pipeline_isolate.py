"""Update pipeline phase 1: isolate the live generation into a staging root."""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from governance_rule.execution.codex_update_pipeline_common import (
        DATABASE_NAME,
        ISOLATION_MARKER,
        PART_NAMES,
        RELEASED_MARKER,
        UPDATE_FLOW_IDENTITY,
        CodexUpdateError,
        IsolatedStage,
        _canonical_codex_root,
        _digest,
        _read_version,
        _same_path,
        _set_read_only,
        _source_foreign_key_violations,
        _write_json,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_update_pipeline_common import (
        DATABASE_NAME,
        ISOLATION_MARKER,
        PART_NAMES,
        RELEASED_MARKER,
        UPDATE_FLOW_IDENTITY,
        CodexUpdateError,
        IsolatedStage,
        _canonical_codex_root,
        _digest,
        _read_version,
        _same_path,
        _set_read_only,
        _source_foreign_key_violations,
        _write_json,
    )


def isolate_generation(codex_root: str | Path, staging_root: str | Path) -> IsolatedStage:
    """Phase 1: copy the live generation into a non-authoritative isolation."""
    root = Path(codex_root).resolve()
    staging = Path(staging_root).resolve()
    if not root.is_dir():
        raise CodexUpdateError("isolate", f"codex root not found: {root}")
    if staging == root or staging.is_relative_to(root):
        raise CodexUpdateError("isolate", "staging root must be outside the codex root")
    database = root / "data" / DATABASE_NAME

    if staging.is_dir() and any(
        (staging / marker).exists()
        for marker in (DATABASE_NAME, ISOLATION_MARKER, RELEASED_MARKER)
    ):
        residue = staging.with_name(
            f"{staging.name}-stale-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
            f"-{uuid.uuid4().hex[:8]}"
        )
        try:
            staging.rename(residue)
        except OSError as error:
            raise CodexUpdateError(
                "isolate", f"stale staging residue not quarantined: {error}"
            ) from error
    staging.mkdir(parents=True, exist_ok=True)
    isolated_database = staging / DATABASE_NAME
    if database.is_file():
        shutil.copy2(database, isolated_database)
    elif _same_path(root, _canonical_codex_root()):
        # Canonical root post-cutover (A173): the live authority is the
        # PostgreSQL schema, so the staged working copy is a
        # non-authoritative export of it.
        from governance_rule.execution.codex_postgresql import (
            export_postgresql_codex,
        )

        export_postgresql_codex(isolated_database)
    else:
        raise CodexUpdateError("isolate", f"codex database not found: {database}")
    _set_read_only(isolated_database, False)

    parts: list[Path] = []
    for name in PART_NAMES:
        source = root / name
        if not source.is_file():
            raise CodexUpdateError("isolate", f"mirror part not found: {name}")
        target = staging / name
        shutil.copy2(source, target)
        _set_read_only(target, False)
        parts.append(target)

    digests = {DATABASE_NAME: _digest(isolated_database)}
    digests.update({name: _digest(root / name) for name in PART_NAMES})
    fence_id = str(uuid.uuid4())
    stage = IsolatedStage(
        fence_id=fence_id,
        codex_root=root,
        staging_root=staging,
        database=isolated_database,
        parts=tuple(parts),
        source_version=_read_version(isolated_database),
        source_digests=digests,
        source_fk_violations=_source_foreign_key_violations(isolated_database),
    )
    _write_json(
        staging / ISOLATION_MARKER,
        {
            "fence_id": fence_id,
            "flow": UPDATE_FLOW_IDENTITY,
            "state": "staged-non-authoritative",
            "source_version": stage.source_version,
            "source_digests": digests,
        },
    )
    return stage
