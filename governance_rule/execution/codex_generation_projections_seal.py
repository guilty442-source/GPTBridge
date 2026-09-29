"""Generation-projection seal/revision bookkeeping + normative surface sync."""
from __future__ import annotations

from typing import Any

from governance_rule.execution.codex_amendment_contract import (
    _seal_preview_from,
    revision_entry_hash,
)

try:
    from governance_rule.execution.codex_generation_projections_base import (
        _SURFACE_LAYERS,
        _count,
        _has_table,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_generation_projections_base import (
        _SURFACE_LAYERS,
        _count,
        _has_table,
    )


def _sync_normative_surface(connection, version: str) -> None:
    """Rebind the normative surface to this generation.

    Existing entries keep their curated layer classification; lifecycle_state
    is resynchronized from the lifecycle registry; active objects missing from
    the surface are appended under the registry-declared layer (articles and
    other unmapped types under ``UNKNOWN``).
    """
    if not _has_table(connection, "current_normative_surface"):
        return
    lifecycle = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            "SELECT provision_type, provision_id, lifecycle_state "
            "FROM provision_lifecycle_status"
        )
    } if _has_table(connection, "provision_lifecycle_status") else {}
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
    rules = (
        connection.execute(
            "SELECT rule_code FROM formal_rule_registry WHERE status<>'withdrawn'"
        ).fetchall()
        if _has_table(connection, "formal_rule_registry")
        else []
    )
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


def _append_revision(connection, version: str, epoch: int,
                     change_id: str, change_scope: str, summary: str) -> str:
    if not _has_table(connection, "revision_history"):
        return "0" * 64
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
    connection,
    version: str,
    epoch: int,
    history_head: str,
) -> None:
    """Seal rows certify the generation as built before they are appended."""
    if not (
        _has_table(connection, "seal_manifest")
        and _has_table(connection, "epoch_seal_manifest")
    ):
        return
    preview = _seal_preview_from(connection)
    provision_count = _count(connection, "provision_lifecycle_status")
    identity_count = _count(connection, "provision_identities")
    lineage_count = _count(connection, "provision_lineage")
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
