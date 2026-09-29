"""Update pipeline phase 2: apply + validate the staged successor change."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping

from governance_rule.execution.audit.architecture_docs import (
    architecture_document_report,
)
from governance_rule.execution.codex_mirror_writer import (
    MirrorRenderError,
    mirror_errors,
    record_mirror_quality_evidence,
    render_mirror_parts,
)
from governance_rule.execution.codex_postgresql_stage import open_codex_store
from governance_rule.execution.codex_update_validation import (
    staged_generation_errors,
)

try:
    from governance_rule.execution.codex_update_pipeline_common import (
        CodexUpdateError,
        IsolatedStage,
        PhaseRecord,
        _set_read_only,
    )
except ImportError:  # flat-script import
    from codex_update_pipeline_common import (
        CodexUpdateError,
        IsolatedStage,
        PhaseRecord,
        _set_read_only,
    )


def _normalize_version(database: Path, version: str | None) -> None:
    if not version:
        return
    with open_codex_store(database, write_back=True) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='codex_version'", (version,)
        )


def architecture_sync_errors() -> list[str]:
    """A537/A538 architecture-artifact atomicity gate (fail-closed).

    Every successor generation synchronizes the architecture documents
    atomically: each top-level canonical tool component owns an
    ``architecture-tool-<id>.md`` document and every document carries a
    title plus a mermaid diagram.  The pipeline never authors prose — a
    missing or defective document denies the staged generation so the
    governor/assistant adds it before publication.  Unreferenced-canonical
    and stale-identifier findings stay informational (reported by the
    read-only diagnostic, never a denial) so governed prose is never
    churned by automation.
    """
    project_root = Path(__file__).resolve().parents[2]
    try:
        report = architecture_document_report(project_root)
    except Exception as error:  # noqa: BLE001 — fail closed, type only
        return [
            "architecture document report unavailable: "
            f"{type(error).__name__}"
        ]
    errors = [str(error) for error in report.get("errors") or ()]
    errors.extend(
        str(gap.get("reason") or gap) for gap in report.get("gaps") or ()
    )
    return errors


def _staged_errors(stage: IsolatedStage, version: str | None) -> list[str]:
    """Basic generation integrity for the staged database (shared gate)."""
    return list(
        staged_generation_errors(
            stage.database.as_posix(),
            version=str(version).strip() if version else None,
            baseline_violations=stage.source_fk_violations,
        )
    )


def _validate_and_render(stage: IsolatedStage, version: str | None) -> list[str]:
    errors = _staged_errors(stage, version)
    if errors:
        return errors
    errors.extend(architecture_sync_errors())
    if errors:
        return errors
    try:
        render_mirror_parts(
            stage.database, stage.staging_root, template_root=stage.codex_root
        )
        record_mirror_quality_evidence(stage.database, stage.staging_root)
        render_mirror_parts(
            stage.database, stage.staging_root, template_root=stage.codex_root
        )
        errors.extend(mirror_errors(stage.database, stage.staging_root))
    except (MirrorRenderError, OSError, ValueError, Exception) as error:
        errors.append(f"staged mirror rendering failed: {error}")
    return errors


def _rebind_projections(
    stage: IsolatedStage, bookkeeping: Mapping[str, str] | None
) -> tuple[dict[str, Any], list[str]]:
    """Rebind a prepared successor's derived projections to the staged
    codex_version (version axis, revision chain, seal/epoch manifests,
    search index, module manifest, normative surface).  Rebuild failures
    return as rejection evidence — never an uncaught exception."""
    from governance_rule.execution.codex_generation_projections import (
        rebuild_generation_bookkeeping,
    )

    try:
        return rebuild_generation_bookkeeping(
            stage.database, **dict(bookkeeping or {})
        ), []
    except (OSError, ValueError, KeyError, Exception) as error:
        return {}, [f"generation bookkeeping rebuild failed: {error}"]


def execute_staged_change(
    stage: IsolatedStage,
    *,
    prepared_database: str | Path | None = None,
    version: str | None = None,
    bookkeeping: Mapping[str, str] | None = None,
) -> PhaseRecord:
    """Phase 2: apply the prepared successor and validate the staged change."""
    if prepared_database is not None:
        prepared = Path(prepared_database).resolve()
        if not prepared.is_file():
            raise CodexUpdateError(
                "execute-change", f"prepared database not found: {prepared}"
            )
        _set_read_only(stage.database, False)
        shutil.copyfile(prepared, stage.database)
    _normalize_version(stage.database, version)
    errors: list[str] = []
    bookkeeping_evidence: dict[str, Any] = {}
    if prepared_database is not None:
        # A prepared successor must pass basic generation integrity before
        # any derived projection is rebound onto it; a corrupted successor
        # is rejected here instead of crashing the rebuild mid-write.
        errors = _staged_errors(stage, version)
        if not errors:
            # A prepared successor is a new generation: rebind every derived
            # projection to the staged codex_version before validation and
            # publication; a failed rebind rejects the phase fail-closed.
            bookkeeping_evidence, rebind_errors = _rebind_projections(
                stage, bookkeeping
            )
            errors.extend(rebind_errors)
    if not errors:
        errors = _validate_and_render(stage, version)
    return PhaseRecord(
        phase="execute-change",
        ok=not errors,
        detail="staged generation validated" if not errors else "staged generation rejected",
        evidence={
            "fence_id": stage.fence_id,
            "version": version or stage.source_version,
            "errors": tuple(errors),
            "bookkeeping": bookkeeping_evidence,
        },
    )
