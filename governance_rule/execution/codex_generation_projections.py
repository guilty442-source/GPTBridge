"""Per-generation projection and bookkeeping rebuild for the amendment pipeline.

Every published codex generation must rebind its derived projections to the
generation's own version identity.  Before this module existed, amendment
executions advanced ``metadata.codex_version`` while ``current_version``,
``current_version_identity``, ``active_provision_binding_version``,
``governance_closure_current_version``, ``revision_history``, ``seal_manifest``,
``epoch_seal_manifest``, ``codex_search_index_manifest``,
``codex_internal_module_manifest``, ``codex_search_document``,
``codex_search_fts*`` and ``current_normative_surface`` stayed bound to the
last hand-built projection generation (the version-axis split reported on
2026-09-25).

:func:`rebuild_generation_bookkeeping` runs inside the isolated staging copy
after the prepared successor is applied and its version is normalized, and
before staged validation renders mirrors.  It recomputes every derived row
from the canonical toolchain (``content_hash`` / ``search_document_hash`` /
``revision_entry_hash`` / ``compute_seal_preview`` in
``codex_amendment_contract``) so the recorded values are verifiably the
digests of the staged generation — rows are never stamped with values that
cannot be recomputed from the staged database.

Seal convention: ``seal_manifest`` / ``epoch_seal_manifest`` rows record the
roots of the generation as published *before* the seal rows themselves are
appended (the seal row certifies the generation; it cannot contain its own
digest).  This matches the historical convention — every recorded seal row
was appended at seal time over the pre-seal content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


from governance_rule.execution.codex_postgresql_stage import open_artifact

try:
    from governance_rule.execution.codex_generation_projections_base import (
        PROJECT_VERSION_KEYS,
        _epoch,
        _read_version,
        _restamp_binding_versions,
        _restamp_metadata,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_generation_projections_base import (
        PROJECT_VERSION_KEYS,
        _epoch,
        _read_version,
        _restamp_binding_versions,
        _restamp_metadata,
    )
try:
    from governance_rule.execution.codex_generation_projections_search import (
        _rebuild_fts,
        _rebuild_module_manifest,
        _rebuild_search_documents,
        _rebuild_search_manifest,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_generation_projections_search import (
        _rebuild_fts,
        _rebuild_module_manifest,
        _rebuild_search_documents,
        _rebuild_search_manifest,
    )
try:
    from governance_rule.execution.codex_generation_projections_seal import (
        _append_revision,
        _append_seal_rows,
        _sync_normative_surface,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_generation_projections_seal import (
        _append_revision,
        _append_seal_rows,
        _sync_normative_surface,
    )


def rebuild_generation_bookkeeping(
    database: str | Path,
    *,
    change_id: str = "",
    change_scope: str = "amendment-execution",
    summary: str = "",
) -> dict[str, Any]:
    """Rebind every derived projection of the staged generation to its version.

    Invoked by the update pipeline after the prepared successor has been
    applied and ``metadata.codex_version`` normalized — the successor version
    is only knowable at this point, so this bookkeeping can never live inside
    a request payload.  All recorded digests are recomputed from the staged
    database with the canonical toolchain.
    """
    artifact = Path(database)
    with open_artifact(artifact, write_back=True) as connection:
        try:
            version = _read_version(connection)
            epoch = _epoch(connection)
            _restamp_metadata(connection, version, epoch)
            _restamp_binding_versions(connection, version)
            doc_count = _rebuild_search_documents(connection, version)
            fts_count = _rebuild_fts(connection)
            _rebuild_module_manifest(connection, version)
            _rebuild_search_manifest(connection, version, fts_count, doc_count)
            _sync_normative_surface(connection, version)
            history_head = _append_revision(
                connection,
                version,
                epoch,
                change_id or f"amendment-execution-{version}",
                change_scope,
                summary or f"Governed amendment execution {version}",
            )
            # Seal rows digest the live staged state (a seal cannot digest
            # its own row); the artifact write-back on exit then carries the
            # sealed generation.
            connection.commit()
            _append_seal_rows(connection, version, epoch, history_head)
            connection.commit()
            return {
                "version": version,
                "epoch": epoch,
                "documents": doc_count,
                "fts_rows": fts_count,
                "revision_head": history_head,
            }
        except Exception:
            connection.rollback()
            raise


__all__ = ["rebuild_generation_bookkeeping"]
