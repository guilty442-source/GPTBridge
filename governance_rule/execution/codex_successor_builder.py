"""Governed Codex successor candidate builder (G69).

The builder consumes a request artifact and produces a candidate database in
an explicit output path.  It never mutates the source database and it is
therefore safe to run against an isolated copy produced by the update
pipeline.  Publication, sealing roots in the authoritative manifest,
``revision_history``/``epoch_seal_manifest`` writes, external signatures and
authority re-anchoring remain governor-side.

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

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from governance_rule.execution.codex_amendment_contract import (
    CONTENT_HASH_ALGORITHM,
    SEAL_PREVIEW_SCHEMA,
    compute_seal_preview,
    content_hash,
    validate_rule_state,
    validate_rule_transition,
)
from governance_rule.execution.codex_amendment_lifecycle import (
    AmendmentLifecycleError,
    CodexAmendmentRequestLedger,
    STATE_REJECTED,
    STATE_SUCCESSOR_BUILT,
    STATE_UNDER_REVIEW,
    load_amendment_request,
)
from governance_rule.execution.codex_update_validation import (
    foreign_key_violations,
    staged_generation_errors,
)

CANDIDATE_MANIFEST_SCHEMA: Final[str] = "gptbridge-codex-candidate-manifest/v1"
FORMAL_RULE_REGISTRY: Final[str] = "formal_rule_registry"
SUCCESSOR_SENTINELS: Final[frozenset[str]] = frozenset(
    {
        "successor",
        "<successor>",
        "<successor-version>",
        "successor_version",
        "next-authoritative-utc-second",
    }
)


class SuccessorBuildError(RuntimeError):
    """Fail-closed candidate construction denial."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SuccessorBuildResult:
    ok: bool
    request_id: str
    output_database: str
    manifest_path: str
    candidate_sha256: str = ""
    applied: tuple[Mapping[str, Any], ...] = ()
    deferred: tuple[Mapping[str, Any], ...] = ()
    errors: tuple[str, ...] = ()
    seal_preview: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "request_id": self.request_id,
            "output_database": self.output_database,
            "manifest_path": self.manifest_path,
            "candidate_sha256": self.candidate_sha256,
            "applied": [dict(item) for item in self.applied],
            "deferred": [dict(item) for item in self.deferred],
            "errors": list(self.errors),
            "seal_preview": dict(self.seal_preview or {}),
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_database(source: Path, output: Path) -> None:
    source_connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro", uri=True
    )
    output_connection = sqlite3.connect(str(output))
    try:
        source_connection.backup(output_connection)
        output_connection.commit()
    finally:
        output_connection.close()
        source_connection.close()


def _source_foreign_key_violations(
    source: Path,
) -> tuple[tuple[str, ...], ...]:
    connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro", uri=True
    )
    try:
        return foreign_key_violations(connection)
    finally:
        connection.close()


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_columns(
    connection: sqlite3.Connection, table: str
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
    connection: sqlite3.Connection, table: str
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
    connection: sqlite3.Connection,
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
    count = connection.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table)} WHERE {where}",
        tuple(key[name] for name in sorted(key)),
    ).fetchone()[0]
    return int(count)


def _existing_row(
    connection: sqlite3.Connection,
    table: str,
    key: Mapping[str, Any],
) -> dict[str, Any] | None:
    columns = _require_table(connection, table)
    names = [str(column["name"]) for column in columns]
    where = " AND ".join(
        f"{_quote_identifier(str(name))} IS ?" for name in sorted(key)
    )
    row = connection.execute(
        f"SELECT {', '.join(_quote_identifier(name) for name in names)} "
        f"FROM {_quote_identifier(table)} WHERE {where}",
        tuple(key[name] for name in sorted(key)),
    ).fetchone()
    return dict(zip(names, row)) if row is not None else None


def _evaluator_codes(connection: sqlite3.Connection) -> frozenset[str]:
    try:
        from governance_rule.execution import formal_rules

        database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        formal_rules.load_formal_rules(database)
        return formal_rules.registered_rule_codes()
    except (ImportError, RuntimeError, OSError, sqlite3.Error) as error:
        raise SuccessorBuildError(
            "FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE", str(error)
        ) from error


def _validate_formal_rule_transition(
    connection: sqlite3.Connection,
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
    connection: sqlite3.Connection,
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
    connection.execute(
        f"INSERT INTO {_quote_identifier(table)} "
        f"({', '.join(_quote_identifier(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        tuple(normalized[name] for name in names),
    )
    return {"action": "insert", "table": table, "row": normalized}


def _update_rows(
    connection: sqlite3.Connection,
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
    connection.execute(
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


def _apply_changes(
    connection: sqlite3.Connection,
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
    connection: sqlite3.Connection, successor_version: str | None
) -> None:
    if not successor_version:
        return
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


def _formal_rule_errors(connection: sqlite3.Connection) -> tuple[str, ...]:
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

        formal_rules.load_formal_rules(Path(connection.execute("PRAGMA database_list").fetchone()[2]))
        evaluator_codes = formal_rules.registered_rule_codes()
    except (ImportError, RuntimeError, OSError, sqlite3.Error):
        return ("FORMAL_RULE_EVALUATOR_REGISTRY_UNAVAILABLE",)
    parity_columns = [
        name for name in ("parity_evidence_id", "parity_status") if name in names
    ]
    selected = ", ".join(
        [_quote_identifier(code_column), _quote_identifier(status_column)]
        + [_quote_identifier(name) for name in parity_columns]
    )
    errors: list[str] = []
    rows = connection.execute(
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
        connection = sqlite3.connect(str(output))
        try:
            _set_candidate_version(connection, successor_version)
            applied, deferred = _apply_changes(
                connection,
                request.payload,
                successor_version=successor_version,
            )
            connection.commit()
            errors = list(
                staged_generation_errors(
                    output.as_posix(),
                    version=successor_version,
                    baseline_violations=baseline_violations,
                )
            )
            errors.extend(_formal_rule_errors(connection))
        finally:
            connection.close()
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
                "external-signatures",
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
    except (AmendmentLifecycleError, SuccessorBuildError, OSError, sqlite3.Error) as error:
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
