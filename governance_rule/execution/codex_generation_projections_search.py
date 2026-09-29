"""Generation-projection search rebuilds: documents, FTS shadow rows, manifests."""
from __future__ import annotations

from governance_rule.execution.codex_amendment_contract import (
    content_hash,
    search_document_hash,
)

try:
    from governance_rule.execution.codex_generation_projections_base import (
        _has_table,
        _provision_text_maps,
        _rows,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_generation_projections_base import (
        _has_table,
        _provision_text_maps,
        _rows,
    )


def _rebuild_search_documents(connection, version: str) -> int:
    """Regenerate codex_search_document from the current generation.

    One document per provision_lifecycle_status row.  Provisions of the four
    governing types render their live text; registry projections
    (closure-definition / registry-rule / formal-rule) render the registered
    synthetic document shape.  Stale snapshots of superseded text are replaced
    — the document store mirrors this generation, not history.
    """
    if not (
        _has_table(connection, "provision_lifecycle_status")
        and _has_table(connection, "codex_search_document")
    ):
        return 0
    text_maps = _provision_text_maps(connection)
    modules = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, module_code "
            "FROM codex_internal_module_membership"
        )
    } if _has_table(connection, "codex_internal_module_membership") else {}
    laws = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, law_code "
            "FROM provision_law_classification"
        )
    } if _has_table(connection, "provision_law_classification") else {}
    # law_code is NOT NULL; provisions without a classification row keep the
    # law recorded by the previous generation, falling back to CODEX_MAIN.
    prior_law = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, law_code "
            "FROM codex_search_document"
        )
    }
    prior_module = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, module_code "
            "FROM codex_search_document"
        )
    }
    lifecycle = connection.execute(
        "SELECT provision_type, provision_id, lifecycle_state "
        "FROM provision_lifecycle_status"
    ).fetchall()
    connection.execute("DELETE FROM codex_search_document")
    staged: list[tuple] = []
    for provision_type, provision_id, lifecycle_state in lifecycle:
        ptype, pid = str(provision_type), str(provision_id)
        if ptype in ("article", "principle", "edict", "sovereign"):
            mapped = text_maps.get((ptype, pid))
            if mapped is None:
                continue
            subject, content = mapped["subject"], mapped["content"]
        else:
            subject = pid
            content = (
                f"{ptype} {pid} resolves normative detail through its "
                "registered owner"
            )
        module_code = (
            modules.get((ptype, pid))
            or prior_module.get((ptype, pid))
            or "CODEX_MODULE_DIRECTORY"
        )
        law_code = laws.get((ptype, pid)) or prior_law.get((ptype, pid)) or "CODEX_MAIN"
        staged.append(
            (
                ptype,
                pid,
                module_code,
                law_code,
                subject,
                content,
                search_document_hash(content),
                str(lifecycle_state),
                version,
            )
        )
    connection.executemany(
        "INSERT INTO codex_search_document (provision_type, provision_id, "
        "module_code, law_code, subject, content, content_hash, "
        "lifecycle_state, version_identity) VALUES (?,?,?,?,?,?,?,?,?)",
        staged,
    )
    return len(staged)


def _rebuild_fts(connection) -> int:
    """Mirror active-lifecycle documents into the FTS projection.

    ``codex_search_fts`` and its ``*_content``/``*_data``/``*_idx``/
    ``*_docsize``/``*_config`` companions are plain exported rows in this
    store; the index internals are reproduced deterministically by building a
    real FTS5 index in a scratch database and copying its storage rows.
    """
    if not (
        _has_table(connection, "codex_search_fts")
        and _has_table(connection, "codex_search_document")
    ):
        return 0
    docs = connection.execute(
        "SELECT provision_type, provision_id, module_code, law_code, subject, "
        "content FROM codex_search_document WHERE lifecycle_state='active' "
        "ORDER BY provision_type, provision_id"
    ).fetchall()
    connection.execute("DELETE FROM codex_search_fts")
    connection.executemany(
        "INSERT INTO codex_search_fts (provision_type, provision_id, "
        "module_code, law_code, subject, content) VALUES (?,?,?,?,?,?)",
        docs,
    )
    # Shadow tables are plain exported rows in the PostgreSQL authority —
    # there is no FTS5 engine here, so the projection is rebuilt
    # deterministically: ``_content`` mirrors document columns 1:1,
    # ``_docsize`` records per-column character lengths as a deterministic
    # blob, and the engine-internal ``_data``/``_idx`` payloads (SQLite FTS5
    # segment encoding) are cleared — they have no consumer or meaning
    # outside the retired engine.  ``_config`` rows are static engine
    # settings and are left untouched.
    shadow_tables = {
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'codex_search_fts_%'"
        )
    }
    if "codex_search_fts_content" in shadow_tables:
        connection.execute("DELETE FROM codex_search_fts_content")
        connection.executemany(
            "INSERT INTO codex_search_fts_content (id, c0, c1, c2, c3, c4, c5) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (index + 1, *(str(value) for value in row))
                for index, row in enumerate(docs)
            ],
        )
    if "codex_search_fts_docsize" in shadow_tables:
        connection.execute("DELETE FROM codex_search_fts_docsize")
        connection.executemany(
            "INSERT INTO codex_search_fts_docsize (id, sz) VALUES (?,?)",
            [
                (
                    index + 1,
                    ",".join(str(len(str(value))) for value in row).encode(
                        "utf-8"
                    ),
                )
                for index, row in enumerate(docs)
            ],
        )
    for engine_internal in ("codex_search_fts_data", "codex_search_fts_idx"):
        if engine_internal in shadow_tables:
            connection.execute(  # sql-ok: fixed FTS-shadow allowlist identifier
                f'DELETE FROM "{engine_internal}"'
            )
    return len(docs)


def _rebuild_module_manifest(connection, version: str) -> None:
    """Recompute each module manifest row from live membership and text."""
    if not (
        _has_table(connection, "codex_internal_module_manifest")
        and _has_table(connection, "codex_internal_module_membership")
    ):
        return
    modules = [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT module_code FROM codex_internal_module_membership "
            "ORDER BY module_code"
        )
    ]
    lifecycle = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, lifecycle_state "
            "FROM provision_lifecycle_status"
        )
    } if _has_table(connection, "provision_lifecycle_status") else {}
    all_membership = _rows(connection, "codex_internal_module_membership")
    all_docs = _rows(connection, "codex_search_document")
    all_deps = _rows(connection, "codex_internal_module_dependency")
    all_lifecycle = _rows(connection, "provision_lifecycle_status")
    connection.execute("DELETE FROM codex_internal_module_manifest")
    staged: list[tuple] = []
    for module in modules:
        membership = [row for row in all_membership if row[2] == module]
        member_keys = sorted((str(r[0]), str(r[1])) for r in membership)
        member_ids = [pid for _ptype, pid in member_keys]
        states = [lifecycle.get(key, "unregistered") for key in member_keys]
        active = sum(1 for s in states if s == "active")
        superseded = sum(1 for s in states if s == "superseded")
        retired = sum(1 for s in states if s == "retired")
        other = len(states) - active - superseded - retired
        docs = [row for row in all_docs if row[2] == module]
        doc_digest = content_hash(docs)
        deps = [row for row in all_deps if row[0] == module or row[1] == module]
        lifecycle_rows = [
            r for r in all_lifecycle if (str(r[0]), str(r[1])) in member_keys
        ]
        classification_rows = [
            r
            for r in _rows(connection, "provision_law_classification")
            if (str(r[0]), str(r[1])) in member_keys
        ]
        resolution_rows = [
            r
            for r in _rows(connection, "provision_reference_resolution_v2")
            if str(r[1]) in member_ids
        ]
        staged.append(
            (
                module,
                version,
                len(membership),
                active,
                superseded,
                content_hash(membership),
                doc_digest,
                content_hash(deps),
                doc_digest,
                "sealed",
                version,
                retired,
                other,
                content_hash(lifecycle_rows),
                content_hash(classification_rows),
                content_hash(resolution_rows),
            )
        )
    connection.executemany(
        "INSERT INTO codex_internal_module_manifest (module_code, "
        "version_identity, provision_count, active_count, "
        "superseded_count, membership_hash, content_hash, "
        "dependency_hash, search_document_hash, status, sealed_at_utc, "
        "retired_count, other_state_count, lifecycle_hash, "
        "classification_hash, successor_resolution_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        staged,
    )


def _rebuild_search_manifest(
    connection, version: str, fts_count: int, doc_count: int
) -> None:
    """Restamp the search index manifest with digests of this generation."""
    if not _has_table(connection, "codex_search_index_manifest"):
        return
    docs = _rows(connection, "codex_search_document")
    fts = _rows(connection, "codex_search_fts")
    alias = _rows(connection, "codex_search_alias")
    modules = _rows(connection, "codex_internal_module_manifest")
    deps = _rows(connection, "codex_internal_module_dependency")
    connection.execute(
        "UPDATE codex_search_index_manifest SET codex_version_identity=?, "
        "authoritative_content_root=?, source_count=?, indexed_count=?, "
        "module_manifest_root=?, index_content_hash=?, built_at_utc=?, "
        "status='current', alias_count=?, alias_hash=?, fts_row_count=?, "
        "fts_content_hash=?, dependency_graph_hash=?",
        (
            version,
            content_hash(docs),
            len(docs),
            doc_count,
            content_hash(modules),
            content_hash(docs),
            version,
            len(alias),
            content_hash(alias),
            fts_count,
            content_hash(fts),
            content_hash(deps),
        ),
    )
