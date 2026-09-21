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
import shutil
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from governance_rule.execution.codex_amendment_contract import (
    SEAL_PREVIEW_SCHEMA,
    compute_seal_preview,
    content_hash,
    validate_rule_state,
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
    staged_generation_errors,
)

CANDIDATE_MANIFEST_SCHEMA: Final[str] = "gptbridge-codex-candidate-manifest/v1"
FORMAL_RULE_REGISTRY: Final[str] = "formal_rule_registry"
SUCCESSOR_SENTINELS: Final[frozenset[str]] = frozenset(
    {"successor", "<successor>", "successor_version"}
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


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    row: Mapping[str, Any],
) -> Mapping[str, Any]:
    columns = _require_table(connection, table)
    normalized = _normalized_row(columns, row, table)
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
        if str(value).strip() in SUCCESSOR_SENTINELS:
            if not successor_version:
                raise SuccessorBuildError(
                    "SUCCESSOR_VERSION_REQUIRED", f"{table}.{name}"
                )
            normalized[name] = successor_version
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
        applied.append(
            _update_rows(
                connection,
                table,
                key,
                fields,
                successor_version=successor_version,
            )
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
                applied.append(_insert_row(connection, registry, row))
            continue
        if action == "update":
            key = item.get("key")
            fields = item.get("set") or item.get("fields")
            if not isinstance(key, Mapping) or not isinstance(fields, Mapping):
                raise SuccessorBuildError("SUCCESSOR_UPDATE_INVALID", registry)
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
    if "key" not in {str(column["name"]) for column in columns}:
        raise SuccessorBuildError("METADATA_KEY_COLUMN_REQUIRED")
    connection.execute(
        "INSERT INTO metadata (key, value) VALUES ('codex_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
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
    errors: list[str] = []
    rows = connection.execute(
        f"SELECT {_quote_identifier(code_column)}, {_quote_identifier(status_column)} "
        f"FROM {_quote_identifier(FORMAL_RULE_REGISTRY)}"
    )
    for code, status in rows:
        rule_code = str(code or "")
        state_errors = validate_rule_state(
            status,
            evaluator_registered=rule_code in evaluator_codes,
            parity_evidence=False,
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
        output.parent.mkdir(parents=True, exist_ok=True)
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
        shutil.copyfile(source, output)
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
                    output.as_posix(), version=successor_version
                )
            )
            errors.extend(_formal_rule_errors(connection))
        finally:
            connection.close()
        if errors:
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
        }
        manifest["manifest_hash"] = content_hash(manifest)
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
