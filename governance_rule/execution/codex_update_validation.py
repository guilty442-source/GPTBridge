"""Staged-generation and mirror quality validation for automatic Codex updates.

法典依據 (published A537/A538 fragments + the A382/A488 amendment flow):
- A538: automatic Codex updates, the five Chinese mirror parts and all
  architecture artifacts must synchronize atomically in one successor
  transaction; the machine architecture registry rebuilds in the same
  generation.
- A537: retired-module cleanup classification, architecture artifacts and the
  runtime deadlines (startup 10s / mandatory test suite 20s / independent
  audit flow 30s) synchronize with each successor generation.
- A382/A488: VALIDATE precedes CERTIFY; publication is atomic; post-publication
  audit verifies the published generation.

This module is the validation half of that flow.  It never writes the Codex.
It measures a staged or live generation so the publisher cannot publish
replacement-character corruption (the defect the 2026-09-17 generations
exhibited: Chinese text stored as U+003F) and so the audit can independently
recompute the same metrics from the live files.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Final, Iterable, Mapping, Sequence

import psycopg

# A run of five or more U+003F characters is treated as replacement damage.
# Legitimate prose question marks are single, so the threshold keeps the
# detector free of false positives on ASCII text.
REPLACEMENT_RUN_THRESHOLD: Final[int] = 5

# Content-bearing tables and their human-language text fields.
TEXT_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("articles", "subject"),
    ("articles", "rule"),
    ("articles", "prohibition"),
    ("articles", "exception"),
    ("sections", "title"),
    ("sections", "summary"),
    ("preamble", "title"),
    ("preamble", "authority_rank"),
    ("preamble", "issuance"),
    ("preamble", "binding_scope"),
    ("principles", "statement"),
    ("edicts", "edict"),
    ("sovereigns", "name"),
    ("sovereigns", "area"),
)


def longest_replacement_run(text: object) -> int:
    """Longest consecutive run of U+003F characters in ``text``."""
    value = str(text or "")
    longest = 0
    current = 0
    for character in value:
        if character == "?":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in text)


def replacement_character_count(text: object) -> int:
    """Count replacement damage: ``?`` that replaced non-ASCII text.

    Legitimate ASCII question marks inside English rule prose (for example
    A359/A360) are punctuation, not mirror damage.  Damage is counted when a
    ``?`` sits in CJK context (CJK character within two characters) or forms
    a run at/over the damage threshold.
    """
    value = str(text or "")
    count = 0
    for index, character in enumerate(value):
        if character != "?":
            continue
        window = value[max(0, index - 2): index + 3]
        if _has_cjk(window) or longest_replacement_run(value[max(0, index - 2): index + 3]) >= REPLACEMENT_RUN_THRESHOLD:
            count += 1
    return count


def is_replacement_damaged(text: object, threshold: int = REPLACEMENT_RUN_THRESHOLD) -> bool:
    return longest_replacement_run(text) >= threshold


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    try:
        return tuple(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        )
    except (sqlite3.Error, psycopg.Error):
        return ()


def find_replacement_damage(
    connection: sqlite3.Connection,
    fields: Iterable[tuple[str, str]] = TEXT_FIELDS,
    *,
    threshold: int = REPLACEMENT_RUN_THRESHOLD,
) -> tuple[str, ...]:
    """Report ``table.field:row-key`` for text fields carrying damage.

    Row keys prefer ``provision_id``/``id``/``position`` when present so the
    finding names the exact provision instead of a bare row number.
    """
    findings: list[str] = []
    columns_by_table: dict[str, tuple[str, ...]] = {}
    for table, field in fields:
        columns = columns_by_table.setdefault(
            table, _table_columns(connection, table)
        )
        if not columns or field not in columns:
            continue
        key = next(
            (name for name in ("provision_id", "id", "name", "position") if name in columns),
            None,
        )
        select = ", ".join((key, field) if key else (field,))
        for row in connection.execute(f"SELECT {select} FROM {table}"):
            row_key = str(row[0]) if key else "?"
            value = row[-1]
            if is_replacement_damaged(value, threshold):
                findings.append(f"{table}.{field}:{row_key}")
    return tuple(findings)


def generation_text_metrics(
    connection: sqlite3.Connection,
    fields: Iterable[tuple[str, str]] = TEXT_FIELDS,
) -> dict[str, int]:
    """Recomputable mirror-quality metrics for one database generation."""
    replacement_count = 0
    loss_fields = 0
    for table, field in fields:
        columns = _table_columns(connection, table)
        if not columns or field not in columns:
            continue
        for row in connection.execute(f"SELECT {field} FROM {table}"):
            value = row[0]
            replacement_count += replacement_character_count(value)
            if is_replacement_damaged(value):
                loss_fields += 1
    return {
        "replacement_character_count": replacement_count,
        "question_loss_field_count": loss_fields,
    }


def foreign_key_violations(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, ...], ...]:
    """Normalized ``PRAGMA foreign_key_check`` rows for baseline comparison."""
    return tuple(
        tuple(str(value) for value in row)
        for row in connection.execute("PRAGMA foreign_key_check")
    )


def validate_database_integrity(
    connection: sqlite3.Connection,
    *,
    baseline_violations: Iterable[Sequence[object]] = (),
) -> tuple[str, ...]:
    """Validate integrity; only NEW foreign-key violations fail.

    The current authority generation carries acknowledged legacy violations
    (pre-genesis history rows were deleted without backfill), so the baseline
    from the live generation is tolerated while any violation the staged
    change introduces is rejected.
    """
    errors: list[str] = []
    check = connection.execute("PRAGMA integrity_check").fetchone()
    status = str(check[0]) if check else "missing"
    if status.casefold() != "ok":
        errors.append(f"staged codex database failed integrity_check: {status}")
    baseline = {
        tuple(str(value) for value in row) for row in baseline_violations
    }
    for row in foreign_key_violations(connection):
        if row not in baseline:
            errors.append(f"staged codex database foreign key violation: {row}")
    for required in ("metadata", "articles"):
        if not _table_columns(connection, required):
            errors.append(f"staged codex database lacks required table: {required}")
    return tuple(errors)


def staged_generation_errors(
    database_path: str,
    *,
    version: str | None = None,
    baseline_violations: Iterable[Sequence[object]] = (),
) -> tuple[str, ...]:
    """Validate one staged database: integrity, version identity, text."""
    errors: list[str] = []
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        return (f"staged codex database cannot be opened: {error}",)
    try:
        errors.extend(
            validate_database_integrity(
                connection, baseline_violations=baseline_violations
            )
        )
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        stored_version = str(metadata.get("codex_version", "")).strip()
        if not stored_version:
            errors.append("staged codex database lacks metadata.codex_version")
        elif version is not None and stored_version != str(version).strip():
            errors.append(
                "staged codex version identity mismatch: "
                f"{stored_version} != {version}"
            )
        for finding in find_replacement_damage(connection):
            errors.append(f"staged codex text replacement damage: {finding}")
    except sqlite3.Error as error:
        errors.append(f"staged codex database validation failed: {error}")
    finally:
        connection.close()
    return tuple(errors)


def mirror_quality_metrics(
    tables: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, int]:
    """Recompute mirror-quality metrics from assembled mirror tables."""
    replacement_count = 0
    loss_fields = 0
    for table, field in TEXT_FIELDS:
        for row in tables.get(table, ()):
            value = row.get(field)
            replacement_count += replacement_character_count(value)
            if is_replacement_damaged(value):
                loss_fields += 1
    return {
        "replacement_character_count": replacement_count,
        "question_loss_field_count": loss_fields,
    }


def mirror_identity_sets(
    tables: Mapping[str, Sequence[Mapping[str, object]]],
    identity_columns: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    """Extract ordered identity sets used for mirror parity comparisons."""
    result: dict[str, tuple[str, ...]] = {}
    for table, column in identity_columns.items():
        values = sorted(
            str(row.get(column))
            for row in tables.get(table, ())
            if row.get(column) is not None
        )
        result[table] = tuple(values)
    return result


def mirror_text_parity_errors(
    connection: sqlite3.Connection,
    mirror_tables: Mapping[str, Sequence[Mapping[str, object]]],
) -> tuple[str, ...]:
    """Compare per-provision mirror text with the database generation.

    A row that carries Chinese text in the database but none in the mirror
    (or whose mirror copy gained replacement damage) means the five-part
    mirror no longer represents the published authority.
    """
    errors: list[str] = []
    columns = _table_columns(connection, "articles")
    if not columns:
        return ("mirror parity check requires the articles table",)
    mirror_rows = {
        str(row.get("provision_id")): row
        for row in mirror_tables.get("articles", ())
        if row.get("provision_id") is not None
    }
    for row in connection.execute("SELECT provision_id, rule FROM articles"):
        provision_id = str(row[0])
        mirror_row = mirror_rows.get(provision_id)
        if mirror_row is None:
            errors.append(f"mirror is missing article: {provision_id}")
            continue
        database_rule = str(row[1] or "")
        mirror_rule = str(mirror_row.get("rule") or "")
        database_cjk = _cjk_count(database_rule)
        mirror_cjk = _cjk_count(mirror_rule)
        if database_cjk and not mirror_cjk:
            errors.append(f"mirror lost Chinese text for article: {provision_id}")
        if is_replacement_damaged(mirror_rule) and not is_replacement_damaged(
            database_rule
        ):
            errors.append(
                f"mirror carries replacement damage absent from the database: {provision_id}"
            )
    return tuple(errors)


def _cjk_count(text: str) -> int:
    return sum(1 for character in text if "\u4e00" <= character <= "\u9fff")


def database_text_metrics_json(metrics: Mapping[str, int]) -> str:
    return json.dumps(dict(metrics), ensure_ascii=True, sort_keys=True)


__all__ = [
    "REPLACEMENT_RUN_THRESHOLD",
    "TEXT_FIELDS",
    "database_text_metrics_json",
    "find_replacement_damage",
    "foreign_key_violations",
    "generation_text_metrics",
    "is_replacement_damaged",
    "longest_replacement_run",
    "mirror_identity_sets",
    "mirror_quality_metrics",
    "mirror_text_parity_errors",
    "replacement_character_count",
    "staged_generation_errors",
    "validate_database_integrity",
]
