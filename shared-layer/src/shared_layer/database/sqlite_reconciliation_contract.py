"""A508/A509 SQLite reconciliation contract (codex-bound).

Binds the failover/degraded SQLite buffer to the machine-readable codex
contract ``sql_reconciliation_contract`` in ``governance_codex.sqlite3``
(read-only):

  * A508 — SQLite is never central official data, cross-module authority or
    shared audit authority.  Every state scope declares numeric maximum
    staleness, maximum rows, maximum operations, an expiry action and a
    bounded fallback classification.
  * A509 — every SQLite fallback declares its target PostgreSQL identity, a
    reconciliation deadline, a conflict policy, a generation and an
    idempotent receipt; unlimited or null bounds are forbidden.

Unreadable, missing, null or unbounded settings raise
:class:`ReconciliationContractError` — the caller fails closed instead of
running an unbounded shadow authority.

Receipt fields follow the codex ``machine_schema_registry`` entry
``RECONCILIATION_RECEIPT_V1`` (``required_fields`` + enum constraints);
unknown receipt fields are rejected by the schema, so the dataclass carries
exactly the registered fields.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, Optional

CONTRACT_CODE: Final[str] = "DEFAULT_SQLITE_TO_POSTGRESQL"
RECEIPT_SCHEMA_CODE: Final[str] = "RECONCILIATION_RECEIPT_V1"

# A508 bounded fallback classification (codex sql_authority_class_registry,
# SQLITE authority classes).  An unclassified fallback is refused.
FALLBACK_STATE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "OWNER_PRIVATE_OPERATIONAL_STATE",
        "OWNER_PRIVATE_CACHE",
        "OWNER_PRIVATE_CHECKPOINT",
        "BOUNDED_DEGRADED_BUFFER",
    }
)

# Conflict policy / RECONCILIATION_RECEIPT_V1 conflict_result enum.
CONFLICT_POLICIES: Final[frozenset[str]] = frozenset(
    {
        "REJECT_ON_CANONICAL_CONFLICT",
        "APPEND_IF_IDEMPOTENCY_KEY_ABSENT",
        "REVALIDATE_AND_REISSUE",
        "OWNER_DOMAIN_MERGE_WITH_DECISION",
    }
)

# A508 expiry action values observed in the codex registries.  Both are
# bounded fail-closed actions; nothing else is accepted.
EXPIRY_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "fail-closed-expire-and-retain-evidence",
        "fail-closed-and-reconcile",
    }
)

# RECONCILIATION_RECEIPT_V1 expiry_cleanup_status enum.
EXPIRY_CLEANUP_STATUSES: Final[frozenset[str]] = frozenset(
    {"pending", "complete", "retained-for-evidence"}
)

RECONCILIATION_RECEIPT_FIELDS: Final[tuple[str, ...]] = (
    "receipt_id",
    "local_record_id",
    "target_postgresql_identity",
    "validated_schema_hash",
    "permission_decision_hash",
    "conflict_result",
    "postgresql_commit_identity",
    "reconciled_at",
    "expiry_cleanup_status",
    "evidence_hash",
)

_UNBOUNDED_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "",
        "none",
        "null",
        "nil",
        "unlimited",
        "unbounded",
        "infinite",
        "infinity",
        "inf",
        "nan",
        "*",
        "any",
    }
)

_CODEX_DB_RELATIVE: Final[tuple[str, ...]] = (
    "governance_rule",
    "codex",
    "data",
    "governance_codex.sqlite3",
)

_CODEX_DB_ENV: Final[str] = "GPTBRIDGE_CODEX_DB"


class ReconciliationContractError(RuntimeError):
    """Raised when the A508/A509 codex contract is missing or unbounded."""


def default_codex_db_path() -> Path:
    """Resolve the codex database path (env override, then workspace copy)."""
    override = str(os.environ.get(_CODEX_DB_ENV, "")).strip()
    if override:
        return Path(override)
    # this file: shared-layer/src/shared_layer/database/<file>
    workspace = Path(__file__).resolve().parents[4]
    return workspace.joinpath(*_CODEX_DB_RELATIVE)


def _bounded_number(
    field: str,
    value: Any,
    *,
    minimum: float,
    exclusive: bool = False,
) -> float:
    if value is None:
        raise ReconciliationContractError(f"SQL_RECONCILIATION_BOUND_NULL:{field}")
    if isinstance(value, bool):
        raise ReconciliationContractError(f"SQL_RECONCILIATION_BOUND_INVALID:{field}")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if text.casefold() in _UNBOUNDED_TOKENS:
            raise ReconciliationContractError(
                f"SQL_RECONCILIATION_BOUND_UNBOUNDED:{field}:{text!r}"
            )
        try:
            number = float(text)
        except (TypeError, ValueError) as error:
            raise ReconciliationContractError(
                f"SQL_RECONCILIATION_BOUND_INVALID:{field}:{value!r}"
            ) from error
    if not math.isfinite(number):
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_BOUND_UNBOUNDED:{field}:{value!r}"
        )
    if exclusive and number <= minimum:
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_BOUND_INVALID:{field}:{number}"
        )
    if not exclusive and number < minimum:
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_BOUND_INVALID:{field}:{number}"
        )
    return number


def _required_text(field: str, value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if not text or text.casefold() in _UNBOUNDED_TOKENS:
        raise ReconciliationContractError(f"SQL_RECONCILIATION_FIELD_MISSING:{field}")
    return text


def _flag(field: str, value: Any) -> bool:
    if value is None:
        raise ReconciliationContractError(f"SQL_RECONCILIATION_FIELD_MISSING:{field}")
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ReconciliationContractError(f"SQL_RECONCILIATION_FIELD_INVALID:{field}:{value!r}")


@dataclass(frozen=True)
class SqliteReconciliationContract:
    """Validated A508/A509 contract row (codex sql_reconciliation_contract)."""

    contract_code: str
    sqlite_scope_code: str
    target_postgresql_identity: str
    state_class: str
    maximum_staleness_seconds: float
    maximum_buffer_rows: int
    maximum_operations: int
    reconciliation_deadline_seconds: float
    conflict_policy: str
    permission_revalidation_required: bool
    idempotency_required: bool
    receipt_schema: str
    expiry_action: str
    version_identity: str
    contract_hash: str
    status: str = "active"

    def assert_covers(self, limits: Any) -> None:
        """Explicit module-private limits may only tighten contract bounds."""
        checks = (
            ("max_pending_count", getattr(limits, "max_pending_count", None),
             self.maximum_buffer_rows),
            ("max_operations", getattr(limits, "max_operations", None),
             self.maximum_operations),
            ("max_staleness_seconds", getattr(limits, "max_staleness_seconds", None),
             self.maximum_staleness_seconds),
            ("reconcile_deadline_seconds",
             getattr(limits, "reconcile_deadline_seconds", None),
             self.reconciliation_deadline_seconds),
        )
        for name, limit, bound in checks:
            if limit is None:
                raise ReconciliationContractError(
                    f"SQL_RECONCILIATION_LIMIT_MISSING:{name}"
                )
            if float(limit) > float(bound):
                raise ReconciliationContractError(
                    f"SQLITE_FALLBACK_BOUND_EXCEEDS_CONTRACT:{name}:{limit}>{bound}"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_code": self.contract_code,
            "sqlite_scope_code": self.sqlite_scope_code,
            "target_postgresql_identity": self.target_postgresql_identity,
            "state_class": self.state_class,
            "maximum_staleness_seconds": self.maximum_staleness_seconds,
            "maximum_buffer_rows": self.maximum_buffer_rows,
            "maximum_operations": self.maximum_operations,
            "reconciliation_deadline_seconds":
                self.reconciliation_deadline_seconds,
            "conflict_policy": self.conflict_policy,
            "permission_revalidation_required":
                self.permission_revalidation_required,
            "idempotency_required": self.idempotency_required,
            "receipt_schema": self.receipt_schema,
            "expiry_action": self.expiry_action,
            "version_identity": self.version_identity,
            "contract_hash": self.contract_hash,
            "status": self.status,
        }


@dataclass(frozen=True)
class ReconciliationReceipt:
    """Typed idempotent receipt (codex RECONCILIATION_RECEIPT_V1)."""

    receipt_id: str
    local_record_id: str
    target_postgresql_identity: str
    validated_schema_hash: str
    permission_decision_hash: str
    conflict_result: str
    postgresql_commit_identity: str
    reconciled_at: str
    expiry_cleanup_status: str
    evidence_hash: str

    def __post_init__(self) -> None:
        for field in RECONCILIATION_RECEIPT_FIELDS:
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ReconciliationContractError(
                    f"RECONCILIATION_RECEIPT_FIELD_MISSING:{field}"
                )
        if self.conflict_result not in CONFLICT_POLICIES:
            raise ReconciliationContractError(
                f"RECONCILIATION_RECEIPT_CONFLICT_RESULT_INVALID:{self.conflict_result}"
            )
        if self.expiry_cleanup_status not in EXPIRY_CLEANUP_STATUSES:
            raise ReconciliationContractError(
                "RECONCILIATION_RECEIPT_EXPIRY_STATUS_INVALID:"
                f"{self.expiry_cleanup_status}"
            )

    def as_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in RECONCILIATION_RECEIPT_FIELDS}

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )


def _canonical_row_hash(row: Mapping[str, Any]) -> str:
    payload = {
        str(key): row[key] for key in sorted(row.keys())
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _read_row(
    connection: sqlite3.Connection, table: str, key_column: str, key_value: str
) -> Optional[sqlite3.Row]:
    try:
        return connection.execute(
            f"SELECT * FROM {table} WHERE {key_column} = ? AND status = 'active'",
            (key_value,),
        ).fetchone()
    except sqlite3.Error:
        return None


def load_reconciliation_contract(
    codex_db_path: Optional[str | Path] = None,
    *,
    contract_code: str = CONTRACT_CODE,
) -> SqliteReconciliationContract:
    """Load and validate the active A508/A509 contract row from the codex.

    Fails closed when the codex database / table / row is unavailable or when
    any declared bound is null, non-finite or unbounded.
    """
    path = Path(codex_db_path) if codex_db_path is not None else default_codex_db_path()
    if not path.is_file():
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_CODEX_MISSING:{path}"
        )
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        )
    except sqlite3.Error as error:
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_CODEX_UNREADABLE:{path}:{error}"
        ) from error
    connection.row_factory = sqlite3.Row
    try:
        row = _read_row(connection, "sql_reconciliation_contract", "contract_code", contract_code)
        if row is None:
            raise ReconciliationContractError(
                f"SQL_RECONCILIATION_CONTRACT_MISSING:{contract_code}"
            )
        v2 = _read_row(
            connection, "sql_reconciliation_contract_v2", "contract_id", contract_code
        )
    except sqlite3.Error as error:
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_CODEX_UNREADABLE:{path}:{error}"
        ) from error
    finally:
        connection.close()

    values = {key: row[key] for key in row.keys()}
    state_class = _required_text(
        "state_class",
        (v2["allowed_state_classes"] if v2 is not None else None)
        or values.get("state", ""),
    )
    # The v1 row's `state` column is a validity state (LOCAL_ONLY_VALID), so
    # the bounded fallback classification comes from the scope registry /
    # authority class registry; use the codex v2 class when available and
    # otherwise the registered bounded buffer class.
    if state_class not in FALLBACK_STATE_CLASSES:
        if state_class == "LOCAL_ONLY_VALID":
            state_class = "BOUNDED_DEGRADED_BUFFER"
        else:
            raise ReconciliationContractError(
                f"SQL_RECONCILIATION_STATE_CLASS_INVALID:{state_class}"
            )

    conflict_policy = _required_text("conflict_policy", values.get("conflict_policy"))
    if conflict_policy not in CONFLICT_POLICIES:
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_CONFLICT_POLICY_INVALID:{conflict_policy}"
        )
    expiry_action = _required_text("expiry_action", values.get("expiry_action"))
    if expiry_action not in EXPIRY_ACTIONS:
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_EXPIRY_ACTION_INVALID:{expiry_action}"
        )
    receipt_schema = _required_text("receipt_schema", values.get("receipt_schema"))
    if receipt_schema != RECEIPT_SCHEMA_CODE:
        raise ReconciliationContractError(
            f"SQL_RECONCILIATION_RECEIPT_SCHEMA_INVALID:{receipt_schema}"
        )
    contract_hash = ""
    if v2 is not None:
        candidate = str(v2["content_hash"] or "").strip()
        if len(candidate) == 64:
            contract_hash = candidate
    if not contract_hash:
        contract_hash = _canonical_row_hash(values)

    return SqliteReconciliationContract(
        contract_code=_required_text("contract_code", values.get("contract_code")),
        sqlite_scope_code=_required_text(
            "sqlite_scope_code", values.get("sqlite_scope_code")
        ),
        target_postgresql_identity=_required_text(
            "target_postgresql_identity", values.get("target_postgresql_identity")
        ),
        state_class=state_class,
        maximum_staleness_seconds=_bounded_number(
            "maximum_staleness_seconds",
            values.get("maximum_staleness_seconds"),
            minimum=0.0,
            exclusive=True,
        ),
        maximum_buffer_rows=int(
            _bounded_number(
                "maximum_buffer_rows",
                values.get("maximum_buffer_rows"),
                minimum=0.0,
                exclusive=True,
            )
        ),
        maximum_operations=int(
            _bounded_number(
                "maximum_operations",
                values.get("maximum_operations"),
                minimum=0.0,
                exclusive=True,
            )
        ),
        reconciliation_deadline_seconds=_bounded_number(
            "reconciliation_deadline_seconds",
            values.get("reconciliation_deadline_seconds"),
            minimum=0.0,
            exclusive=True,
        ),
        conflict_policy=conflict_policy,
        permission_revalidation_required=_flag(
            "permission_revalidation", values.get("permission_revalidation")
        ),
        idempotency_required=_flag(
            "idempotency_required", values.get("idempotency_required")
        ),
        receipt_schema=receipt_schema,
        expiry_action=expiry_action,
        version_identity=_required_text(
            "version_identity", values.get("version_identity")
        ),
        contract_hash=contract_hash,
    )


__all__ = [
    "CONFLICT_POLICIES",
    "CONTRACT_CODE",
    "EXPIRY_ACTIONS",
    "EXPIRY_CLEANUP_STATUSES",
    "FALLBACK_STATE_CLASSES",
    "RECEIPT_SCHEMA_CODE",
    "RECONCILIATION_RECEIPT_FIELDS",
    "ReconciliationContractError",
    "ReconciliationReceipt",
    "SqliteReconciliationContract",
    "default_codex_db_path",
    "load_reconciliation_contract",
]
