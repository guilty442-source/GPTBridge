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

import sqlite3
from pathlib import Path
from typing import Any

from governance_rule.execution.codex_amendment_contract import (
    content_hash,
    compute_seal_preview,
    revision_entry_hash,
    search_document_hash,
)

PROJECT_VERSION_KEYS: tuple[str, ...] = (
    "current_version",
    "active_provision_binding_version",
    "governance_closure_current_version",
)

# Search-document projection contract: one document per
# provision_lifecycle_status row; content reproduces the governing text of the
# provision under a per-type template; hash = search_document_hash(content).
_DOC_TYPES = ("article", "principle", "edict", "sovereign",
              "closure-definition", "registry-rule", "formal-rule")

_SURFACE_LAYERS = {
    "article": "UNKNOWN",
    "principle": "SPECIAL_LAW_DOMAIN_RULE",
    "edict": "SPECIAL_LAW_DOMAIN_RULE",
    "sovereign": "SPECIAL_LAW_DOMAIN_RULE",
    "closure-definition": "CLOSURE_AGGREGATION",
    "registry-rule": "REGISTRY_FACT",
    "formal-rule": "FORMAL-RULE",
}


def _rows(connection: sqlite3.Connection, table: str, order_by: str = "") -> list[list]:
    sql = f'SELECT * FROM "{table}"'
    if order_by:
        sql += f" ORDER BY {order_by}"
    return [list(row) for row in connection.execute(sql)]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]


def _read_version(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='codex_version'"
    ).fetchone()
    if not row or not str(row[0]).strip():
        raise RuntimeError("generation bookkeeping requires metadata.codex_version")
    return str(row[0]).strip()


def _epoch(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='current_version_epoch'"
    ).fetchone()
    return int(row[0]) if row else 2


def _restamp_metadata(connection: sqlite3.Connection, version: str, epoch: int) -> None:
    connection.executemany(
        "UPDATE metadata SET value=? WHERE key=?",
        [(version, key) for key in PROJECT_VERSION_KEYS],
    )
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='current_version_identity'",
        (f"E{epoch}:{version}",),
    )


def _restamp_binding_versions(connection: sqlite3.Connection, version: str) -> None:
    """Rebind per-provision current binding pointers to this generation."""
    for table in ("provision_lifecycle_status", "effective_provisions"):
        columns = _columns(connection, table)
        if "current_binding_version" in columns:
            connection.execute(  # sql-ok: identifier from the fixed allowlist above; once per table
                f'UPDATE "{table}" SET current_binding_version=?', (version,)
            )


def _provision_text_maps(connection: sqlite3.Connection) -> dict[str, dict]:
    """Load governing text for every lifecycle-registered provision."""
    maps: dict[str, dict] = {}
    for row in connection.execute(
        "SELECT provision_id, subject, rule, prohibition, exception FROM articles"
    ):
        maps[("article", str(row[0]))] = {
            "subject": str(row[1]),
            "content": f"{row[1]}\n{row[2]}\n{row[3]}\n{row[4]}",
        }
    for row in connection.execute(
        "SELECT provision_id, statement, binding FROM principles"
    ):
        maps[("principle", str(row[0]))] = {
            "subject": str(row[0]),
            "content": f"{row[1]} {row[2]}",
        }
    for row in connection.execute(
        "SELECT provision_id, area, edict, immutability FROM edicts"
    ):
        maps[("edict", str(row[0]))] = {
            "subject": str(row[1]),
            "content": f"{row[1]} {row[2]} {row[3]}",
        }
    for row in connection.execute(
        "SELECT sovereign_id, area, rank, basis FROM sovereigns"
    ):
        maps[("sovereign", str(row[0]))] = {
            "subject": str(row[0]),
            "content": f"{row[0]} {row[1]} {row[2]} {row[3]}",
        }
    return maps


def _rebuild_search_documents(connection: sqlite3.Connection, version: str) -> int:
    """Regenerate codex_search_document from the current generation.

    One document per provision_lifecycle_status row.  Provisions of the four
    governing types render their live text; registry projections
    (closure-definition / registry-rule / formal-rule) render the registered
    synthetic document shape.  Stale snapshots of superseded text are replaced
    — the document store mirrors this generation, not history.
    """
    text_maps = _provision_text_maps(connection)
    modules = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, module_code "
            "FROM codex_internal_module_membership"
        )
    }
    laws = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, law_code "
            "FROM provision_law_classification"
        )
    }
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


def _rebuild_fts(connection: sqlite3.Connection) -> int:
    """Mirror active-lifecycle documents into the FTS projection.

    ``codex_search_fts`` and its ``*_content``/``*_data``/``*_idx``/
    ``*_docsize``/``*_config`` companions are plain exported rows in this
    store; the index internals are reproduced deterministically by building a
    real FTS5 index in a scratch database and copying its storage rows.
    """
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
    shadow_tables = [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'codex_search_fts_%'"
        )
    ]
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.execute(
            "CREATE VIRTUAL TABLE codex_search_fts USING fts5("
            "provision_type, provision_id, module_code, law_code, subject, "
            "content)"
        )
        scratch.executemany(
            "INSERT INTO codex_search_fts (provision_type, provision_id, "
            "module_code, law_code, subject, content) VALUES (?,?,?,?,?,?)",
            docs,
        )
        for shadow in shadow_tables:
            inner = shadow[len("codex_search_fts_"):]
            try:
                rows = scratch.execute(
                    f'SELECT * FROM "codex_search_fts_{inner}"'
                ).fetchall()
            except sqlite3.Error:
                continue
            staged_columns = _columns(connection, shadow)
            width = len(staged_columns)
            connection.execute(  # sql-ok: identifier derived from sqlite_master shadow-table allowlist
                f'DELETE FROM "{shadow}"'
            )
            padded_rows = [
                list(row)[:width] + [None] * (width - len(row))
                for row in rows
            ]
            connection.executemany(  # sql-ok: identifier/column list from staged table metadata
                f'INSERT INTO "{shadow}" ({", ".join(staged_columns)}) '
                f'VALUES ({", ".join("?" for _ in staged_columns)})',
                padded_rows,
            )
    finally:
        scratch.close()
    return len(docs)


def _rebuild_module_manifest(connection: sqlite3.Connection, version: str) -> None:
    """Recompute each module manifest row from live membership and text."""
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
    }
    all_membership = _rows(connection, "codex_internal_module_membership")
    all_docs = _rows(connection, "codex_search_document")
    all_deps = _rows(connection, "codex_internal_module_dependency")
    all_lifecycle = _rows(connection, "provision_lifecycle_status")
    all_classification = _rows(connection, "provision_law_classification")
    all_resolution = _rows(connection, "provision_reference_resolution_v2")
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
            r for r in all_classification if (str(r[0]), str(r[1])) in member_keys
        ]
        resolution_rows = [
            r for r in all_resolution if str(r[1]) in member_ids
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
    connection: sqlite3.Connection, version: str, fts_count: int, doc_count: int
) -> None:
    """Restamp the search index manifest with digests of this generation."""
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


def _sync_normative_surface(connection: sqlite3.Connection, version: str) -> None:
    """Rebind the normative surface to this generation.

    Existing entries keep their curated layer classification; lifecycle_state
    is resynchronized from the lifecycle registry; active objects missing from
    the surface are appended under the registry-declared layer (articles and
    other unmapped types under ``UNKNOWN``).
    """
    lifecycle = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, lifecycle_state "
            "FROM provision_lifecycle_status"
        )
    }
    surface = connection.execute(
        "SELECT surface_entry_id, object_type, object_identity "
        "FROM current_normative_surface"
    ).fetchall()
    existing = {(str(r[1]), str(r[2])) for r in surface}
    state_updates = [
        (lifecycle.get((str(otype), str(oid))), _eid)
        for _eid, otype, oid in surface
        if lifecycle.get((str(otype), str(oid))) is not None
    ]
    connection.executemany(
        "UPDATE current_normative_surface SET lifecycle_state=? "
        "WHERE surface_entry_id=?",
        state_updates,
    )
    connection.execute(
        "UPDATE current_normative_surface SET version_identity=?", (version,)
    )
    to_add = [
        (ptype, pid, state)
        for (ptype, pid), state in lifecycle.items()
        if state == "active" and (ptype, pid) not in existing
    ]
    insert_sql = (
        "INSERT INTO current_normative_surface (surface_entry_id, "
        "surface_layer, object_type, object_identity, lifecycle_state, "
        "default_search_visible, version_identity, status) "
        "VALUES (?,?,?,?,?,?,?,?)"
    )
    connection.executemany(
        insert_sql,
        [
            (
                f"{ptype}:{pid}",
                _SURFACE_LAYERS.get(ptype, "UNKNOWN"),
                ptype,
                pid,
                state,
                1,
                version,
                "current",
            )
            for ptype, pid, state in to_add
        ],
    )
    # Formal rules registered without a lifecycle row still belong on the
    # surface: they are current normative objects of this generation.
    rules = connection.execute(
        "SELECT rule_code FROM formal_rule_registry WHERE status<>'withdrawn'"
    ).fetchall()
    rule_keys = {("formal-rule", str(r[0])) for r in rules}
    connection.executemany(
        insert_sql,
        [
            (f"formal-rule:{pid}", "FORMAL-RULE", "formal-rule", pid,
             "active", 1, version, "current")
            for _ptype, pid in sorted(
                rule_keys - existing - set(to_add_keys(to_add))
            )
        ],
    )


def to_add_keys(to_add):
    return {(p, i) for p, i, _s in to_add}


def _append_revision(connection: sqlite3.Connection, version: str, epoch: int,
                     change_id: str, change_scope: str, summary: str) -> str:
    row = connection.execute(
        "SELECT sequence, entry_hash FROM revision_history "
        "ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(row[0]) + 1 if row else 1
    previous_hash = str(row[1]) if row else "0" * 64
    fields: dict[str, Any] = {
        "sequence": sequence,
        "change_id": change_id,
        "version": version,
        "recorded_date": version[:10],
        "change_scope": change_scope,
        "summary": summary,
        "previous_hash": previous_hash,
        "version_epoch": epoch,
        "recorded_at_utc": version,
        "timestamp_status": "verified",
        "timestamp_migration_evidence": "amendment-pipeline-generation-bookkeeping",
    }
    fields["entry_hash"] = revision_entry_hash(fields)
    connection.execute(
        "INSERT INTO revision_history (sequence, change_id, version, "
        "recorded_date, change_scope, summary, previous_hash, entry_hash, "
        "version_epoch, recorded_at_utc, timestamp_status, "
        "timestamp_migration_evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(fields[name] for name in (
            "sequence", "change_id", "version", "recorded_date", "change_scope",
            "summary", "previous_hash", "entry_hash", "version_epoch",
            "recorded_at_utc", "timestamp_status", "timestamp_migration_evidence",
        )),
    )
    return str(fields["entry_hash"])


def _append_seal_rows(
    connection: sqlite3.Connection,
    database: Path,
    version: str,
    epoch: int,
    history_head: str,
) -> None:
    """Seal rows certify the generation as built before they are appended."""
    preview = compute_seal_preview(database)
    provision_count = connection.execute(
        "SELECT COUNT(*) FROM provision_lifecycle_status"
    ).fetchone()[0]
    identity_count = connection.execute(
        "SELECT COUNT(*) FROM provision_identities"
    ).fetchone()[0]
    lineage_count = connection.execute(
        "SELECT COUNT(*) FROM provision_lineage"
    ).fetchone()[0]
    certification = "sealed-governed-certification"
    connection.execute(
        "INSERT INTO seal_manifest (version, history_head, provision_count, "
        "identity_count, lineage_count, certification_state, content_root, "
        "identity_root, full_root, version_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            version,
            history_head,
            provision_count,
            identity_count,
            lineage_count,
            certification,
            preview["content_root"],
            preview["identity_root"],
            preview["full_root"],
            epoch,
        ),
    )
    connection.execute(
        "INSERT INTO epoch_seal_manifest (version_epoch, version, "
        "version_identity, history_head, provision_count, identity_count, "
        "lineage_count, certification_state, content_root, identity_root, "
        "full_root, legacy_history_head) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            epoch,
            version,
            f"E{epoch}:{version}",
            history_head,
            provision_count,
            identity_count,
            lineage_count,
            certification,
            preview["content_root"],
            preview["identity_root"],
            preview["full_root"],
            None,
        ),
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
    database = Path(database)
    connection = sqlite3.connect(str(database))
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
        # The seal preview opens its own read-only connection, so the rebuilt
        # projections must be committed first; seal rows then certify exactly
        # that committed state (a seal cannot digest its own row).
        connection.commit()
        _append_seal_rows(connection, database, version, epoch, history_head)
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
    finally:
        connection.close()


__all__ = ["rebuild_generation_bookkeeping"]
