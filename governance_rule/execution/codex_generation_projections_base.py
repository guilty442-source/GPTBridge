"""Generation-projection base: contract constants and low-level store helpers."""
from __future__ import annotations


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


def _has_table(connection, table: str) -> bool:
    return bool(_columns(connection, table))


def _rows(connection, table: str, order_by: str = "") -> list[list]:
    if not _has_table(connection, table):
        return []
    sql = f'SELECT * FROM "{table}"'
    if order_by:
        sql += f" ORDER BY {order_by}"
    return [list(row) for row in connection.execute(sql)]


def _columns(connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]


def _count(connection, table: str) -> int:
    if not _has_table(connection, table):
        return 0
    return int(
        connection.execute(  # sql-ok: identifier from the fixed projection-table set; name is schema-internal
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
    )


def _read_version(connection) -> str:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='codex_version'"
    ).fetchone()
    if not row or not str(row[0]).strip():
        raise RuntimeError("generation bookkeeping requires metadata.codex_version")
    return str(row[0]).strip()


def _epoch(connection) -> int:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='current_version_epoch'"
    ).fetchone()
    return int(row[0]) if row else 2


def _restamp_metadata(connection, version: str, epoch: int) -> None:
    connection.executemany(
        "UPDATE metadata SET value=? WHERE key=?",
        [(version, key) for key in PROJECT_VERSION_KEYS],
    )
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='current_version_identity'",
        (f"E{epoch}:{version}",),
    )


def _restamp_binding_versions(connection, version: str) -> None:
    """Rebind per-provision current binding pointers to this generation."""
    for table in ("provision_lifecycle_status", "effective_provisions"):
        columns = _columns(connection, table)
        if "current_binding_version" in columns:
            connection.execute(  # sql-ok: identifier from the fixed allowlist above; once per table
                f'UPDATE "{table}" SET current_binding_version=?', (version,)
            )


def _provision_text_maps(connection) -> dict[str, dict]:
    """Load governing text for every lifecycle-registered provision."""
    maps: dict[str, dict] = {}
    if _has_table(connection, "articles"):
        for row in connection.execute(
            "SELECT provision_id, subject, rule, prohibition, exception FROM articles"
        ):
            maps[("article", str(row[0]))] = {
                "subject": str(row[1]),
                "content": f"{row[1]}\n{row[2]}\n{row[3]}\n{row[4]}",
            }
    if _has_table(connection, "principles"):
        for row in connection.execute(
            "SELECT provision_id, statement, binding FROM principles"
        ):
            maps[("principle", str(row[0]))] = {
                "subject": str(row[0]),
                "content": f"{row[1]} {row[2]}",
            }
    if _has_table(connection, "edicts"):
        for row in connection.execute(
            "SELECT provision_id, area, edict, immutability FROM edicts"
        ):
            maps[("edict", str(row[0]))] = {
                "subject": str(row[1]),
                "content": f"{row[1]} {row[2]} {row[3]}",
            }
    if _has_table(connection, "sovereigns"):
        for row in connection.execute(
            "SELECT sovereign_id, area, rank, basis FROM sovereigns"
        ):
            maps[("sovereign", str(row[0]))] = {
                "subject": str(row[0]),
                "content": f"{row[0]} {row[1]} {row[2]} {row[3]}",
            }
    return maps
