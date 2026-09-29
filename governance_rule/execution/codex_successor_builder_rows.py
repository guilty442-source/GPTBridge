"""Successor builder row machinery: identifier quoting, row normalization, insert/update."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Mapping

from governance_rule.execution.codex_amendment_contract import (
    content_hash,
    validate_rule_transition,
)

try:
    from governance_rule.execution.codex_successor_builder_common import (
        FORMAL_RULE_REGISTRY,
        SUCCESSOR_SENTINELS,
        SuccessorBuildError,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_successor_builder_common import (
        FORMAL_RULE_REGISTRY,
        SUCCESSOR_SENTINELS,
        SuccessorBuildError,
    )


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_columns(
    connection, table: str
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    return tuple(
        {
            "name": str(row[1]),
            "type": str(row[2] or ""),
            "notnull": bool(row[3]),
            "default": row[4],
            "pk": int(row[5] or 0),
        }
        for row in rows
    )


def _require_table(
    connection, table: str
) -> tuple[dict[str, Any], ...]:
    columns = _table_columns(connection, table)
    if not columns:
        raise SuccessorBuildError("CANDIDATE_TABLE_MISSING", table)
    return columns


def _normalized_row(
    columns: Sequence[Mapping[str, Any]],
    row: Mapping[str, Any],
    table: str,
    *,
    require_primary: bool = True,
) -> dict[str, Any]:
    names = {str(column["name"]) for column in columns}
    normalized = {str(key): value for key, value in row.items()}
    if table == FORMAL_RULE_REGISTRY and "rule_code" in names and "rule_id" in normalized:
        normalized["rule_code"] = normalized.pop("rule_id")
    unknown = sorted(set(normalized) - names)
    if unknown:
        raise SuccessorBuildError(
            "CANDIDATE_COLUMN_UNKNOWN", f"{table}:{','.join(unknown)}"
        )
    primary = [str(column["name"]) for column in columns if int(column["pk"])]
    missing_primary = [
        name
        for name in primary
        if require_primary and str(normalized.get(name) or "").strip() == ""
    ]
    if missing_primary:
        raise SuccessorBuildError(
            "CANDIDATE_PRIMARY_KEY_INCOMPLETE",
            f"{table}:{','.join(missing_primary)}",
        )
    return normalized


def _substitute_successor(
    value: Any,
    *,
    successor_version: str | None,
    context: str,
) -> Any:
    if isinstance(value, str) and value.strip() in SUCCESSOR_SENTINELS:
        if not successor_version:
            raise SuccessorBuildError("SUCCESSOR_VERSION_REQUIRED", context)
        return successor_version
    if isinstance(value, Mapping):
        return {
            str(key): _substitute_successor(
                item,
                successor_version=successor_version,
                context=f"{context}.{key}",
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _substitute_successor(
                item,
                successor_version=successor_version,
                context=context,
            )
            for item in value
        ]
    return value


def _existing_count(
    connection,
    table: str,
    key: Mapping[str, Any],
) -> int:
    if not key:
        raise SuccessorBuildError("CANDIDATE_KEY_REQUIRED", table)
    columns = _require_table(connection, table)
    names = {str(column["name"]) for column in columns}
    unknown = sorted(set(str(name) for name in key) - names)
    if unknown:
        raise SuccessorBuildError(
            "CANDIDATE_KEY_COLUMN_UNKNOWN", f"{table}:{','.join(unknown)}"
        )
    where = " AND ".join(
        f"{_quote_identifier(str(name))} IS ?" for name in sorted(key)
    )
    count = connection.execute(  # sql-ok: identifiers composed via _quote_identifier
        f"SELECT COUNT(*) FROM {_quote_identifier(table)} WHERE {where}",
        tuple(key[name] for name in sorted(key)),
    ).fetchone()[0]
    return int(count)


def _existing_row(
    connection,
    table: str,
    key: Mapping[str, Any],
) -> dict[str, Any] | None:
    columns = _require_table(connection, table)
    names = [str(column["name"]) for column in columns]
    where = " AND ".join(
        f"{_quote_identifier(str(name))} IS ?" for name in sorted(key)
    )
    row = connection.execute(  # sql-ok: identifiers composed via _quote_identifier
        f"SELECT {', '.join(_quote_identifier(name) for name in names)} "
        f"FROM {_quote_identifier(table)} WHERE {where}",
        tuple(key[name] for name in sorted(key)),
    ).fetchone()
    return dict(zip(names, row)) if row is not None else None


def _evaluator_codes(connection) -> frozenset[str]:
    try:
        from governance_rule.execution import formal_rules

        database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        formal_rules.load_formal_rules(database)
        return formal_rules.registered_rule_codes()
    except (ImportError, RuntimeError, OSError) as error:
        raise SuccessorBuildError(
            "FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE", str(error)
        ) from error


def _validate_formal_rule_transition(
    connection,
    key: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> None:
    if "status" not in fields:
        return
    row = _existing_row(connection, FORMAL_RULE_REGISTRY, key)
    if row is None:
        return
    rule_code = str(key.get("rule_code") or key.get("rule_id") or "")
    parity_evidence = bool(
        fields.get("parity_evidence_id")
        or row.get("parity_evidence_id")
        or str(fields.get("parity_status") or row.get("parity_status") or "")
        .strip()
        .upper()
        == "VERIFIED"
    )
    errors = validate_rule_transition(
        row.get("status"),
        fields.get("status"),
        evaluator_registered=rule_code in _evaluator_codes(connection),
        parity_evidence=parity_evidence,
    )
    if errors:
        raise SuccessorBuildError(
            "RULE_STATE_TRANSITION_INVALID",
            f"{rule_code or content_hash(key)}:{','.join(errors)}",
        )


def _insert_row(
    connection,
    table: str,
    row: Mapping[str, Any],
    *,
    successor_version: str | None,
) -> Mapping[str, Any]:
    columns = _require_table(connection, table)
    normalized = _normalized_row(columns, row, table)
    for name, value in list(normalized.items()):
        normalized[name] = _substitute_successor(
            value,
            successor_version=successor_version,
            context=f"{table}.{name}",
        )
    primary = [str(column["name"]) for column in columns if int(column["pk"])]
    identity = (
        {name: normalized[name] for name in primary}
        if primary
        else normalized
    )
    if _existing_count(connection, table, identity):
        raise SuccessorBuildError(
            "CANDIDATE_DUPLICATE_ROW", f"{table}:{content_hash(identity)}"
        )
    names = sorted(normalized)
    connection.execute(  # sql-ok: identifiers composed via _quote_identifier
        f"INSERT INTO {_quote_identifier(table)} "
        f"({', '.join(_quote_identifier(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        tuple(normalized[name] for name in names),
    )
    return {"action": "insert", "table": table, "row": normalized}


def _update_rows(
    connection,
    table: str,
    key: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    successor_version: str | None,
) -> Mapping[str, Any]:
    columns = _require_table(connection, table)
    normalized = _normalized_row(
        columns, fields, table, require_primary=False
    )
    for name, value in list(normalized.items()):
        normalized[name] = _substitute_successor(
            value,
            successor_version=successor_version,
            context=f"{table}.{name}",
        )
    count = _existing_count(connection, table, key)
    if count != 1:
        raise SuccessorBuildError(
            "CANDIDATE_ROW_NOT_UNIQUE", f"{table}:{count}"
        )
    assignments = ", ".join(
        f"{_quote_identifier(name)} = ?" for name in sorted(normalized)
    )
    where = " AND ".join(
        f"{_quote_identifier(str(name))} IS ?" for name in sorted(key)
    )
    connection.execute(  # sql-ok: identifiers composed via _quote_identifier
        f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE {where}",
        tuple(normalized[name] for name in sorted(normalized))
        + tuple(key[name] for name in sorted(key)),
    )
    return {
        "action": "update",
        "table": table,
        "key": dict(key),
        "fields": normalized,
    }
