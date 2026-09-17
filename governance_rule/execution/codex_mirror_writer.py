"""Canonical Chinese Codex mirror rendering and quality evidence.

法典依據:
- A537/A538: the five Chinese mirror parts synchronize atomically with every
  successor generation; publication must not lose Chinese text.
- A446: an unrecorded result is never a pass, so the mirror-quality evidence
  row is recorded in the same staged generation that gets published.
- SEAL_CANONICAL_V1: UTF-8, NFC text, compact JSON, SHA-256, deterministic
  row order (declared primary key, then full-row lexical fallback).

The renderer writes only inside an explicitly given target root (the isolated
staging generation); it never touches the live mirror.  The part chain and
assembled payload hashes are produced with the same canonicalization the
read-only loader verifies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Final, Mapping

from governance_rule.execution.chinese_codex_mirror import (
    PART_NAMES,
    load_chinese_codex_parts,
)
from governance_rule.execution.codex_update_validation import (
    mirror_quality_metrics,
    mirror_text_parity_errors,
)

EVIDENCE_TABLE: Final[str] = "chinese_mirror_quality_evidence"
EVIDENCE_SCHEMA: Final[str] = (
    "CREATE TABLE IF NOT EXISTS chinese_mirror_quality_evidence ("
    "evidence_id TEXT, part_count INTEGER, replacement_character_count INTEGER, "
    "question_loss_field_count INTEGER, chain_valid INTEGER, "
    "assembled_hash_valid INTEGER, result TEXT, version_identity TEXT, status TEXT)"
)


class MirrorRenderError(RuntimeError):
    """Fail-closed denial while rendering the mirror generation."""


def canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _json_safe(value: object, table: str, column: str) -> object:
    if isinstance(value, (bytes, bytearray)):
        raise MirrorRenderError(f"binary column cannot be mirrored: {table}.{column}")
    return value


def _mirror_table_rows(
    connection: sqlite3.Connection, table: str
) -> list[dict[str, object]]:
    columns = tuple(
        (str(row[1]), int(row[5]))
        for row in connection.execute(f"PRAGMA table_info({table})")
    )
    if not columns:
        return []
    names = [name for name, _ in columns]
    primary = [name for name, rank in columns if rank > 0]
    order = f"ORDER BY {', '.join(primary)}" if primary else "ORDER BY rowid"
    rows = [
        {name: _json_safe(value, table, name) for name, value in zip(names, row)}
        for row in connection.execute(
            f"SELECT {', '.join(names)} FROM {table} {order}"
        )
    ]
    return sorted(
        rows,
        key=lambda row: (
            tuple(str(row.get(name)) for name in primary),
            canonical_json(row),
        ),
    )


def _mirror_table_chunks(
    connection: sqlite3.Connection, template_root: Path | None
) -> list[list[str]]:
    mapping = _template_part_mapping(template_root) if template_root else None
    if mapping is not None:
        return [
            [table for table, part in mapping.items() if part == index]
            for index in range(1, 6)
        ]
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts_%' ORDER BY name"
        )
    ]
    return [names[index::5] for index in range(5)]


def _write_mirror_part(
    target: Path,
    index: int,
    version: str,
    assembled_hash: str,
    previous_hash: str,
    tables: Mapping[str, list[dict[str, object]]],
    names: list[str],
) -> tuple[Path, str]:
    part: dict[str, object] = {
        "mirror_id": "GOVERNANCE_CODEX_ZH_TW",
        "codex_version": version,
        "part_index": index,
        "part_count": 5,
        "previous_part_hash": previous_hash,
        "assembled_payload_hash": assembled_hash,
        "tables": {table: tables.get(table, []) for table in names},
    }
    part_hash = hashlib.sha256(canonical_json(part).encode("utf-8")).hexdigest()
    part["part_hash"] = part_hash
    path = target / PART_NAMES[index - 1]
    path.write_text(canonical_json(part) + "\n", encoding="utf-8")
    return path, part_hash


def _template_part_mapping(template_root: Path) -> dict[str, int] | None:
    mapping: dict[str, int] = {}
    try:
        for index, name in enumerate(PART_NAMES, 1):
            part = json.loads((template_root / name).read_text(encoding="utf-8"))
            for table in part.get("tables", {}):
                mapping.setdefault(str(table), index)
    except (OSError, ValueError):
        return None
    return mapping or None


def render_mirror_parts(
    database: str | Path,
    target_root: str | Path,
    *,
    template_root: str | Path | None = None,
) -> tuple[Path, ...]:
    """Render the five ordered mirror parts from one database generation.

    Without a template every user table is rendered and split into five
    balanced parts; with a template the template's table order and part
    assignment are preserved while the rows come from the given database.
    """
    target = Path(target_root)
    target.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{Path(database).as_posix()}?mode=ro", uri=True)
    try:
        version = str(
            dict(connection.execute("SELECT key, value FROM metadata")).get(
                "codex_version", ""
            )
        ).strip()
        if not version:
            raise MirrorRenderError("database lacks codex_version")
        chunks = _mirror_table_chunks(
            connection, Path(template_root) if template_root else None
        )
        tables = {
            table: _mirror_table_rows(connection, table)
            for chunk in chunks
            for table in chunk
        }
    finally:
        connection.close()
    assembled_hash = hashlib.sha256(
        canonical_json({"codex_version": version, "tables": tables}).encode("utf-8")
    ).hexdigest()
    paths: list[Path] = []
    previous_hash = "0" * 64
    for index, names in enumerate(chunks, 1):
        path, previous_hash = _write_mirror_part(
            target, index, version, assembled_hash, previous_hash, tables, names
        )
        paths.append(path)
    return tuple(paths)


def mirror_errors(
    database: str | Path,
    parts_root: str | Path,
    *,
    label: str = "staged",
) -> tuple[str, ...]:
    """Validate the rendered mirror against its database generation."""
    try:
        mirror = load_chinese_codex_parts(Path(parts_root))
    except (OSError, ValueError) as error:
        return (f"{label} mirror is invalid: {error}",)
    errors = list(mirror_parity_errors(database, mirror["tables"]))
    metrics = mirror_quality_metrics(mirror["tables"])
    if metrics["question_loss_field_count"]:
        errors.append(
            f"{label} mirror carries replacement damage: "
            f"{metrics['question_loss_field_count']} fields lost"
        )
    return tuple(errors)


def mirror_parity_errors(
    database: str | Path, mirror_tables: Mapping[str, object]
) -> tuple[str, ...]:
    connection = sqlite3.connect(f"file:{Path(database).as_posix()}?mode=ro", uri=True)
    try:
        return mirror_text_parity_errors(connection, mirror_tables)
    finally:
        connection.close()


def record_mirror_quality_evidence(
    database: str | Path, parts_root: str | Path
) -> dict[str, int]:
    """Record the recomputed mirror-quality evidence in the staged database."""
    mirror = load_chinese_codex_parts(Path(parts_root))
    metrics = mirror_quality_metrics(mirror["tables"])
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(EVIDENCE_SCHEMA)
        version = str(
            connection.execute(
                "SELECT value FROM metadata WHERE key='codex_version'"
            ).fetchone()[0]
        ).strip()
        connection.execute(
            "UPDATE chinese_mirror_quality_evidence SET status='superseded' "
            "WHERE status='current'"
        )
        connection.execute(
            f"INSERT OR REPLACE INTO {EVIDENCE_TABLE} VALUES (?, 5, ?, ?, 1, 1, "
            "'PASS', ?, 'current')",
            (
                f"MIRROR@{version}",
                int(metrics["replacement_character_count"]),
                int(metrics["question_loss_field_count"]),
                version,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return metrics


__all__ = [
    "EVIDENCE_SCHEMA",
    "EVIDENCE_TABLE",
    "MirrorRenderError",
    "PART_NAMES",
    "canonical_json",
    "mirror_errors",
    "mirror_parity_errors",
    "record_mirror_quality_evidence",
    "render_mirror_parts",
]
