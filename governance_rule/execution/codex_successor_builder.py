"""Governed Codex successor candidate builder (G69).

The builder consumes a request artifact and produces a candidate database in
an explicit output path.  It never mutates the source database and it is
therefore safe to run against an isolated copy produced by the update
pipeline.  Publication, sealing roots in the authoritative manifest,
``revision_history``/``epoch_seal_manifest`` writes and authority
re-anchoring remain governor-side; the flow uses no external signatures —
the unanimous five-sovereign audit certificate closes the seal.

Supported worker-side request actions are deliberately narrow:

* ``changes`` — update exactly one existing row in a named table;
* ``proposed_successors[].action == "insert"`` — insert explicit registry
  rows when every supplied column exists and the row's declared primary-key
  identity is complete;
* ``proposed_successors[].action == "update"`` — update rows through an
  explicit ``key`` mapping plus ``set``/``fields`` values;
* ``rebind`` and ``provision`` proposals are recorded as deferred work for
  the governor-side amendment package, not invented by the worker.

The candidate manifest records the request hash, lineage, applied and
deferred work, deterministic seal preview and the governor-only steps that
remain.  Every schema or lineage mismatch is fail-closed.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping


from governance_rule.execution.codex_amendment_contract import (
    CONTENT_HASH_ALGORITHM,
    SEAL_PREVIEW_SCHEMA,
    compute_seal_preview,
    content_hash,
)
from governance_rule.execution.codex_amendment_lifecycle import (
    AmendmentLifecycleError,
    CodexAmendmentRequestLedger,
    STATE_REJECTED,
    STATE_SUCCESSOR_BUILT,
    STATE_UNDER_REVIEW,
    load_amendment_request,
)
from governance_rule.execution.codex_postgresql_stage import open_artifact
from governance_rule.execution.codex_update_validation import (
    staged_generation_errors,
)


try:
    from governance_rule.execution.codex_successor_builder_common import (
        CANDIDATE_MANIFEST_SCHEMA,
        FORMAL_RULE_REGISTRY,
        SuccessorBuildError,
        SuccessorBuildResult,
        _atomic_json,
        _copy_database,
        _file_sha256,
        _source_foreign_key_violations,
        _utc_now,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_successor_builder_common import (
        CANDIDATE_MANIFEST_SCHEMA,
        FORMAL_RULE_REGISTRY,
        SuccessorBuildError,
        SuccessorBuildResult,
        _atomic_json,
        _copy_database,
        _file_sha256,
        _source_foreign_key_violations,
        _utc_now,
    )
try:
    from governance_rule.execution.codex_successor_builder_apply import (
        _apply_changes,
        _formal_rule_errors,
        _set_candidate_version,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_successor_builder_apply import (
        _apply_changes,
        _formal_rule_errors,
        _set_candidate_version,
    )


def build_successor(
    request_path: str | Path,
    source_database: str | Path,
    output_database: str | Path,
    *,
    ledger: CodexAmendmentRequestLedger,
    successor_version: str | None = None,
    expected_current_version: str | None = None,
    expected_revision_sequence: int | None = None,
) -> SuccessorBuildResult:
    """Build one validated candidate and record its lineage evidence."""
    request_id = ""
    output_created = False
    manifest_path = Path(output_database).with_suffix(".candidate-manifest.json")
    try:
        request = load_amendment_request(request_path)
        request_id = request.request_id
        source = Path(source_database).resolve()
        output = Path(output_database).resolve()
        if not source.is_file():
            raise SuccessorBuildError("SOURCE_DATABASE_MISSING", str(source))
        if source == output:
            raise SuccessorBuildError("CANDIDATE_MUST_NOT_OVERWRITE_SOURCE")
        if output.exists():
            raise SuccessorBuildError("CANDIDATE_OUTPUT_EXISTS", str(output))
        if manifest_path.exists():
            raise SuccessorBuildError(
                "CANDIDATE_MANIFEST_EXISTS", str(manifest_path)
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        baseline_violations = _source_foreign_key_violations(source)
        record = ledger.begin(
            request_path,
            current_version=expected_current_version,
            expected_revision_sequence=expected_revision_sequence,
        )
        if record.state == "submitted":
            ledger.transition(request_id, STATE_UNDER_REVIEW)
        elif record.state != STATE_UNDER_REVIEW:
            raise SuccessorBuildError(
                "REQUEST_NOT_UNDER_REVIEW", f"{request_id}:{record.state}"
            )
        _copy_database(source, output)
        output_created = True
        with open_artifact(output, write_back=True) as connection:
            _set_candidate_version(connection, successor_version)
            applied, deferred = _apply_changes(
                connection,
                request.payload,
                successor_version=successor_version,
            )
            connection.commit()
            errors = list(_formal_rule_errors(connection))
        errors.extend(
            staged_generation_errors(
                output.as_posix(),
                version=successor_version,
                baseline_violations=baseline_violations,
            )
        )
        if errors:
            try:
                output.unlink()
                output_created = False
            except (FileNotFoundError, OSError):
                pass
            ledger.transition(
                request_id,
                STATE_REJECTED,
                evidence={"errors": errors},
            )
            return SuccessorBuildResult(
                False,
                request_id,
                str(output),
                str(manifest_path),
                errors=tuple(errors),
            )
        seal_preview = compute_seal_preview(output)
        candidate_sha256 = _file_sha256(output)
        manifest = {
            "schema": CANDIDATE_MANIFEST_SCHEMA,
            "request_id": request_id,
            "request_hash": request.request_hash,
            "lineage_key": request.lineage_key,
            "source_database_sha256": _file_sha256(source),
            "candidate_sha256": candidate_sha256,
            "successor_version": successor_version or "",
            "predecessor": dict(request.predecessor),
            "scope": list(request.scope),
            "applied": applied,
            "deferred": deferred,
            "seal_preview_schema": SEAL_PREVIEW_SCHEMA,
            "seal_preview": seal_preview,
            "governor_only": [
                "seal_manifest",
                "epoch_seal_manifest",
                "revision_history",
                "atomic-publication",
                "authority-reanchor",
            ],
            "created_at": _utc_now(),
            "manifest_hash_algorithm": CONTENT_HASH_ALGORITHM,
            "manifest_hash_excludes": ["manifest_hash"],
        }
        manifest["manifest_hash"] = content_hash(
            {key: value for key, value in manifest.items() if key != "manifest_hash"}
        )
        _atomic_json(manifest_path, manifest)
        ledger.transition(
            request_id,
            STATE_SUCCESSOR_BUILT,
            evidence={
                "candidate_sha256": candidate_sha256,
                "manifest_path": str(manifest_path),
                "seal_preview": seal_preview,
            },
        )
        return SuccessorBuildResult(
            True,
            request_id,
            str(output),
            str(manifest_path),
            candidate_sha256=candidate_sha256,
            applied=tuple(applied),
            deferred=tuple(deferred),
            seal_preview=seal_preview,
        )
    except (AmendmentLifecycleError, SuccessorBuildError, OSError) as error:
        if output_created:
            try:
                Path(output_database).unlink()
            except (FileNotFoundError, OSError):
                pass
        if request_id:
            try:
                record = ledger.load_record(request_id)
                if record and record.get("state") not in {
                    STATE_REJECTED,
                    "executed",
                    "withdrawn",
                }:
                    ledger.transition(
                        request_id,
                        STATE_REJECTED,
                        evidence={"error": str(error)},
                    )
            except AmendmentLifecycleError:
                pass
        return SuccessorBuildResult(
            False,
            request_id,
            str(Path(output_database).resolve()),
            str(manifest_path),
            errors=(str(error),),
        )


__all__ = [
    "CANDIDATE_MANIFEST_SCHEMA",
    "FORMAL_RULE_REGISTRY",
    "SuccessorBuildError",
    "SuccessorBuildResult",
    "build_successor",
]
