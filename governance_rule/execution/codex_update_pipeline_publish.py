"""Update pipeline phases 3-5: wire, release isolation, frontend refresh."""
from __future__ import annotations

import shutil
import stat
from pathlib import Path
from typing import Any, Callable, Mapping

from governance_rule.execution.chinese_codex_mirror import PART_NAMES
from governance_rule.execution.codex_mirror_writer import mirror_errors
from governance_rule.execution.codex_update_validation import (
    staged_generation_errors,
)

try:
    from governance_rule.execution.codex_update_pipeline_common import (
        DATABASE_NAME,
        FRONTEND_REFRESH_CHANNELS,
        ISOLATION_MARKER,
        RELEASED_MARKER,
        REFRESH_REQUEST,
        UPDATE_FLOW_IDENTITY,
        CodexUpdateError,
        IsolatedStage,
        PhaseRecord,
        _atomic_replace,
        _canonical_codex_root,
        _digest,
        _read_version,
        _same_path,
        _write_json,
    )
except ImportError:  # flat-script import
    from codex_update_pipeline_common import (
        DATABASE_NAME,
        FRONTEND_REFRESH_CHANNELS,
        ISOLATION_MARKER,
        RELEASED_MARKER,
        REFRESH_REQUEST,
        UPDATE_FLOW_IDENTITY,
        CodexUpdateError,
        IsolatedStage,
        PhaseRecord,
        _atomic_replace,
        _canonical_codex_root,
        _digest,
        _read_version,
        _same_path,
        _write_json,
    )


def wire_generation(stage: IsolatedStage) -> PhaseRecord:
    """Phase 3: atomically publish the staged database and mirror parts."""
    published: dict[str, str] = {}
    for name in PART_NAMES:
        if not (stage.staging_root / name).is_file():
            raise CodexUpdateError("wire", f"staged mirror part missing: {name}")
    # The PostgreSQL authority is the single live codex: it may only be
    # wired from the canonical codex root.  Synthetic roots (tests,
    # rehearsals) must never republish the shared authority.
    canonical = _same_path(stage.codex_root, _canonical_codex_root())
    if canonical:
        from governance_rule.execution.codex_postgresql import (
            import_codex_artifact,
            verify_sql_parity,
        )

        postgres_state: Any = import_codex_artifact(stage.database)
        postgres_parity: Any = verify_sql_parity(stage.database)
        if postgres_parity["result"] != "PASS":
            raise CodexUpdateError("wire", "PostgreSQL codex parity verification failed")
    else:
        postgres_state = {"skipped": "non-canonical-codex-root"}
        postgres_parity = {"result": "SKIPPED"}
    published_database = stage.codex_root / "data" / DATABASE_NAME
    if canonical and not published_database.is_file():
        # A173 residue deletion: the canonical codex root no longer carries a
        # sqlite file — the PostgreSQL import above IS the publication.
        pass
    else:
        _atomic_replace(stage.database, published_database)
    published[DATABASE_NAME] = _digest(stage.database)
    for name in PART_NAMES:
        staged_part = stage.staging_root / name
        _atomic_replace(staged_part, stage.codex_root / name)
        published[name] = _digest(stage.codex_root / name)
    return PhaseRecord(
        phase="wire",
        ok=True,
        detail="published generation wired atomically",
        evidence={
            "fence_id": stage.fence_id,
            "published": published,
            "postgresql": postgres_state,
            "postgresql_parity": postgres_parity,
        },
    )


def _published_database(stage: IsolatedStage) -> Path:
    """The published generation to verify: the codex-root sqlite when it
    exists, otherwise the staged copy that was imported into PostgreSQL."""
    published = stage.codex_root / "data" / DATABASE_NAME
    if published.is_file():
        return published
    return stage.database


def _published_version(stage: IsolatedStage) -> str:
    if _same_path(stage.codex_root, _canonical_codex_root()):
        try:
            from governance_rule.execution.codex_postgresql import (
                authority_state,
            )

            return str(authority_state().get("codex_version") or "")
        except Exception:
            pass
    return _read_version(stage.codex_root / "data" / DATABASE_NAME)


def _published_generation_errors(stage: IsolatedStage) -> tuple[str, ...]:
    codex_root = stage.codex_root
    database = _published_database(stage)
    errors = list(
        staged_generation_errors(
            database.as_posix(), baseline_violations=stage.source_fk_violations
        )
    )
    published_paths: list[Path] = [codex_root / name for name in PART_NAMES]
    if database != stage.database:
        # The codex-root sqlite exists and is a published artifact; the
        # staged scratch copy is exempt from the read-only check.
        published_paths.insert(0, database)
    for path in published_paths:
        if path.stat().st_mode & stat.S_IWRITE:
            errors.append(f"published artifact is writable: {path.name}")
    errors.extend(mirror_errors(database, codex_root, label="published"))
    return tuple(errors)


def release_isolation(stage: IsolatedStage) -> PhaseRecord:
    """Phase 4: verify the published generation and release the fence."""
    errors = _published_generation_errors(stage)
    marker = stage.staging_root / ISOLATION_MARKER
    if marker.exists():
        marker.unlink()
    _write_json(
        stage.staging_root / RELEASED_MARKER,
        {
            "fence_id": stage.fence_id,
            "flow": UPDATE_FLOW_IDENTITY,
            "state": "released",
            "version": _published_version(stage),
            "errors": errors,
        },
    )
    return PhaseRecord(
        phase="release-isolation",
        ok=not errors,
        detail="isolation fence released" if not errors else "release verification failed",
        evidence={"fence_id": stage.fence_id, "errors": errors},
    )


def frontend_refresh(
    stage: IsolatedStage,
    notify: Callable[[Mapping[str, object]], Any] | None = None,
) -> PhaseRecord:
    """Phase 5: emit the frontend connection refresh request."""
    version = _published_version(stage)
    payload: dict[str, object] = {
        "contract": "codex-generation-published",
        "flow": UPDATE_FLOW_IDENTITY,
        "fence_id": stage.fence_id,
        "version": version,
        "channels": list(FRONTEND_REFRESH_CHANNELS),
        "action": "reconnect-and-reload-authority",
    }
    delivered = False
    if notify is not None:
        notify(payload)
        delivered = True
    _write_json(stage.staging_root / REFRESH_REQUEST, payload)
    return PhaseRecord(
        phase="frontend-refresh",
        ok=True,
        detail=(
            "refresh request delivered through the governed hook"
            if delivered
            else "refresh request recorded; no governed notification hook wired"
        ),
        evidence={"version": version, "delivered": delivered},
    )


def _discard_staging(stage: IsolatedStage) -> None:
    shutil.rmtree(stage.staging_root, ignore_errors=True)
