"""Successor builder application: changes application, version stamping, rule checks."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Mapping

from governance_rule.execution.codex_amendment_contract import (
    validate_rule_state,
)

try:
    from governance_rule.execution.codex_successor_builder_common import (
        FORMAL_RULE_REGISTRY,
        SuccessorBuildError,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_successor_builder_common import (
        FORMAL_RULE_REGISTRY,
        SuccessorBuildError,
    )
try:
    from governance_rule.execution.codex_successor_builder_rows import (
        _insert_row,
        _quote_identifier,
        _require_table,
        _table_columns,
        _update_rows,
        _validate_formal_rule_transition,
    )
except ImportError:  # flat-script import: execution/ directly on sys.path
    from codex_successor_builder_rows import (
        _insert_row,
        _quote_identifier,
        _require_table,
        _table_columns,
        _update_rows,
        _validate_formal_rule_transition,
    )


def _apply_changes(
    connection,
    payload: Mapping[str, Any],
    *,
    successor_version: str | None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    applied: list[Mapping[str, Any]] = []
    deferred: list[Mapping[str, Any]] = []
    for index, item in enumerate(payload.get("changes") or ()):
        if not isinstance(item, Mapping):
            raise SuccessorBuildError("CHANGE_NOT_AN_OBJECT", str(index))
        table = str(item.get("table") or "").strip()
        key = item.get("key")
        field = str(item.get("field") or "").strip()
        if not table or not isinstance(key, Mapping) or not field:
            raise SuccessorBuildError("CHANGE_CONTRACT_INVALID", str(index))
        fields = {field: item.get("proposed")}
        also = item.get("also")
        if isinstance(also, Mapping):
            fields.update(dict(also))
        if table == FORMAL_RULE_REGISTRY:
            _validate_formal_rule_transition(connection, key, fields)
        applied.append(
            _update_rows(
                connection,
                table,
                key,
                fields,
                successor_version=successor_version,
            )
        )
    proposed_change = payload.get("proposed_change")
    if isinstance(proposed_change, Mapping):
        table = str(proposed_change.get("table") or "").strip()
        operation = str(proposed_change.get("operation") or "").strip().lower()
        action = str(proposed_change.get("action") or "").strip().lower()
        rows = proposed_change.get("rows")
        if (
            table
            and "/" not in table
            and (action == "insert" or "insert" in operation)
            and isinstance(rows, Sequence)
            and not isinstance(rows, (str, bytes))
        ):
            for row in rows:
                if not isinstance(row, Mapping):
                    raise SuccessorBuildError("SUCCESSOR_ROW_INVALID", table)
                applied.append(
                    _insert_row(
                        connection,
                        table,
                        row,
                        successor_version=successor_version,
                    )
                )
        else:
            deferred.append(
                {
                    "action": "deferred",
                    "reason": "non-canonical proposed_change requires governor normalization",
                    "payload": proposed_change,
                }
            )
    for proposal_key in ("proposed_repair", "proposed_resolution"):
        proposal = payload.get(proposal_key)
        if isinstance(proposal, Mapping):
            deferred.append(
                {
                    "action": "deferred",
                    "reason": f"{proposal_key} requires governor normalization",
                    "payload": proposal,
                }
            )
    successors = payload.get("proposed_successors") or ()
    if isinstance(successors, Mapping):
        successors = (successors,)
    for index, item in enumerate(successors):
        if not isinstance(item, Mapping):
            raise SuccessorBuildError("SUCCESSOR_NOT_AN_OBJECT", str(index))
        registry = str(item.get("registry") or item.get("table") or "").strip()
        action = str(item.get("action") or "").strip().lower()
        if not registry:
            deferred.append(
                {
                    "action": "deferred",
                    "index": index,
                    "reason": "governor-provision-or-artifact-assignment",
                    "payload": item,
                }
            )
            continue
        if action == "insert":
            rows = item.get("rows")
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                raise SuccessorBuildError("SUCCESSOR_ROWS_REQUIRED", registry)
            for row in rows:
                if not isinstance(row, Mapping):
                    raise SuccessorBuildError("SUCCESSOR_ROW_INVALID", registry)
                applied.append(
                    _insert_row(
                        connection,
                        registry,
                        row,
                        successor_version=successor_version,
                    )
                )
            continue
        if action == "update":
            key = item.get("key")
            fields = item.get("set") or item.get("fields")
            if not isinstance(key, Mapping) or not isinstance(fields, Mapping):
                raise SuccessorBuildError("SUCCESSOR_UPDATE_INVALID", registry)
            if registry == FORMAL_RULE_REGISTRY:
                _validate_formal_rule_transition(connection, key, fields)
            applied.append(
                _update_rows(
                    connection,
                    registry,
                    key,
                    fields,
                    successor_version=successor_version,
                )
            )
            continue
        deferred.append(
            {
                "action": "deferred",
                "index": index,
                "registry": registry,
                "requested_action": action or "unspecified",
                "reason": "artifact rebind, derived-registry rebuild or governor-side row set",
                "payload": item,
            }
        )
    singular = payload.get("proposed_successor")
    if isinstance(singular, Mapping):
        deferred.append(
            {
                "action": "deferred",
                "reason": "governor-provision-id-and-normative-text-assignment",
                "payload": singular,
            }
        )
    return applied, deferred


def _set_candidate_version(
    connection, successor_version: str | None
) -> None:
    if not successor_version:
        return
    from governance_rule.execution.codex_repository import codex_version_units

    try:
        codex_version_units(successor_version)
    except ValueError as error:
        raise SuccessorBuildError(
            "SUCCESSOR_VERSION_INVALID", str(successor_version)
        ) from error
    columns = _require_table(connection, "metadata")
    names = {str(column["name"]) for column in columns}
    if "key" not in names or "value" not in names:
        raise SuccessorBuildError("METADATA_CONTRACT_INCOMPLETE")
    cursor = connection.execute(
        "UPDATE metadata SET value=? WHERE key='codex_version'",
        (successor_version,),
    )
    if cursor.rowcount == 0:
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES ('codex_version', ?)",
            (successor_version,),
        )


def _formal_rule_errors(connection) -> tuple[str, ...]:
    columns = _table_columns(connection, FORMAL_RULE_REGISTRY)
    if not columns:
        return ()
    names = {str(column["name"]) for column in columns}
    status_column = "status" if "status" in names else ""
    code_column = next(
        (name for name in ("rule_code", "rule_id", "code") if name in names),
        "",
    )
    if not status_column or not code_column:
        return ("FORMAL_RULE_REGISTRY_CONTRACT_INCOMPLETE",)
    try:
        from governance_rule.execution import formal_rules

        formal_rules.load_formal_rules()
        evaluator_codes = formal_rules.registered_rule_codes()
    except (ImportError, RuntimeError, OSError):
        return ("FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE",)
    parity_columns = [
        name for name in ("parity_evidence_id", "parity_status") if name in names
    ]
    selected = ", ".join(
        [_quote_identifier(code_column), _quote_identifier(status_column)]
        + [_quote_identifier(name) for name in parity_columns]
    )
    errors: list[str] = []
    rows = connection.execute(  # sql-ok: identifiers composed via _quote_identifier
        f"SELECT {selected} FROM {_quote_identifier(FORMAL_RULE_REGISTRY)}"
    )
    for row in rows:
        code, status = row[0], row[1]
        stored = dict(zip(parity_columns, row[2:]))
        parity_evidence = bool(stored.get("parity_evidence_id")) or (
            str(stored.get("parity_status") or "").strip().upper() == "VERIFIED"
        )
        rule_code = str(code or "")
        state_errors = validate_rule_state(
            status,
            evaluator_registered=rule_code in evaluator_codes,
            parity_evidence=parity_evidence,
        )
        for error in state_errors:
            errors.append(f"{FORMAL_RULE_REGISTRY}:{rule_code}:{error}")
    return tuple(errors)
