"""Failover shared layer store — PostgreSQL ↔ SQLite degraded closure.

Policy-gated failover wrapper around the canonical PostgreSQL
``PostgresSharedLayerStore`` and the bounded local fallback
``LocalSharedLayerStore``.

Closure rules (A8/E21 + A44/E30 + A512):

1. PostgreSQL healthy → route to PostgreSQL (canonical authority).
2. PostgreSQL failure (circuit open or write exception) AND policy
   permits AND the A508/A509 codex contract is loaded AND the SQLite
   scope boundary (A512) verifies → route to ``LocalSharedLayerStore``.
   Each fallback write accumulates a bounded pending change in a local
   ``reconcile_state`` table; SQLite never becomes the canonical authority.
3. PostgreSQL recovers → ``reconcile()`` invokes the one-directional
   ``ReconcileService`` (SQLite → PostgreSQL).  Pending rows are cleared
   only after reconciliation succeeds and a typed receipt is persisted.
4. Conflicts (same version, different hash) are flagged as ``conflict``
   and left for an authorized owner to resolve.  The failover store
   never decides a conflict winner.

Contract posture (fail-closed):

* **A508** — the degraded buffer enforces numeric ``max_pending_count``,
  ``max_operations``, ``max_staleness_seconds`` and registration-based
  ``reconcile_deadline_seconds``.  Expiry executes the codex
  ``expiry_action`` (``fail-closed-expire-and-retain-evidence``): rows are
  marked ``expired``, a local audit event is retained and the store
  refuses new fallback writes.  Missing bounds disable failover.
* **A509** — reconciliation requires a verified central PostgreSQL target
  identity (``POSTGRESQL_CENTRAL`` per the codex ``sql_reconciliation_contract``),
  a generation that matches every pending row, permission revalidation,
  and emits a typed idempotent receipt (``RECONCILIATION_RECEIPT_V1``)
  persisted with the row.
* **A512** — file-backed fallback buffers must present a verified
  ``SqliteScopeBinding`` (path allowlist + process identity + locator
  scope + owner-only ACL expectation).  Unverifiable boundaries fail
  closed; ``:memory:`` test doubles without a declared path remain
  unbound (never a production configuration).

Idempotency: ``submit_request`` accepts an optional ``idempotency_key``
in the payload.  Duplicate submits with the same key are ignored by the
local fallback, preventing duplicate requests during failover flapping.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .database.sqlite_reconciliation_contract import (
    EXPIRY_ACTIONS,
    FALLBACK_STATE_CLASSES,
    ReconciliationContractError,
    ReconciliationReceipt,
    SqliteReconciliationContract,
    load_reconciliation_contract,
)
from .resilient_circuit import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ResilientStoreConfig,
)
from .security.generation import (
    GenerationLedger,
    SecurityGenerationError,
    assert_generation_current,
)
from .security.sqlite_scope import (
    SqliteScopeBinding,
    SqliteScopeError,
    assert_binding,
)

_logger = logging.getLogger("gptbridge.failover_store")

# Decision-owner injection (P8 unit boundary): the reconcile decision surface
# lives in the runtime unit (core_system.data_reconciliation). The shared
# layer must not import upward, so the owner registers a factory here; the
# composition root (integration.data_platform) is the production registrar.
_RECONCILE_SERVICE_FACTORY = None


def register_reconcile_service_factory(factory) -> None:
    """Register the decision-owner reconcile service factory.

    ``factory(local_conn, pg_connection) -> service`` must yield an object
    exposing ``reconcile_module(module_id, batch_size=...)``. Called once by
    the runtime composition root; reconciliation stays fail-closed when no
    factory is registered.
    """
    global _RECONCILE_SERVICE_FACTORY
    _RECONCILE_SERVICE_FACTORY = factory


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_timestamp(value: Any) -> Optional[float]:
    """Parse an ISO timestamp into epoch seconds; ``None`` when unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _default_pg_identity_resolver(connection: Any) -> Optional[str]:
    """Resolve the declared target PostgreSQL identity of a connection.

    Returns ``None`` when the connection cannot prove its identity — the
    caller must then fail closed.  A real psycopg connection can be tagged
    through ``options``/``application_name`` or by wrapping it with a
    resolver supplied by the assembly layer.
    """
    if connection is None:
        return None
    for attribute in (
        "target_identity",
        "postgresql_target_identity",
        "gptbridge_target_identity",
    ):
        value = getattr(connection, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    info = getattr(connection, "info", None)
    if info is not None:
        parameters = None
        try:
            if hasattr(info, "get_parameters"):
                parameters = info.get_parameters()
            elif hasattr(info, "dsn_parameters"):
                parameters = info.dsn_parameters
        except Exception:  # pragma: no cover - driver specific
            parameters = None
        if isinstance(parameters, dict):
            for key in ("target_identity", "gptbridge_target", "application_name"):
                value = parameters.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    dsn = getattr(connection, "dsn", None)
    if isinstance(dsn, str):
        match = re.search(
            r"(?:target_identity|gptbridge_target)=([A-Za-z0-9_.\-]+)", dsn
        )
        if match:
            return match.group(1)
    return None


@dataclass
class FailoverStats:
    """Observability counters for the failover store."""

    pg_calls: int = 0
    pg_successes: int = 0
    pg_failures: int = 0
    fallback_calls: int = 0
    fallback_successes: int = 0
    fallback_failures: int = 0
    circuit_opens: int = 0
    reconciliations_run: int = 0
    reconciliation_pushed: int = 0
    reconciliation_pulled: int = 0
    reconciliation_conflicts: int = 0
    reconciliation_skipped: int = 0
    fail_closed_events: int = 0
    operations_attempted: int = 0
    expired_pending: int = 0
    rejected_rows: int = 0
    receipts_issued: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def record_pg_success(self) -> None:
        with self._lock:
            self.pg_calls += 1
            self.pg_successes += 1

    def record_pg_failure(self) -> None:
        with self._lock:
            self.pg_calls += 1
            self.pg_failures += 1

    def record_fallback_success(self) -> None:
        with self._lock:
            self.fallback_calls += 1
            self.fallback_successes += 1

    def record_fallback_failure(self) -> None:
        with self._lock:
            self.fallback_calls += 1
            self.fallback_failures += 1

    def record_circuit_open(self) -> None:
        with self._lock:
            self.circuit_opens += 1

    def record_fail_closed(self) -> None:
        with self._lock:
            self.fail_closed_events += 1

    def record_operation(self) -> None:
        with self._lock:
            self.operations_attempted += 1

    def record_expired(self, count: int = 1) -> None:
        with self._lock:
            self.expired_pending += count

    def record_rejected(self, count: int = 1) -> None:
        with self._lock:
            self.rejected_rows += count

    def record_receipt(self) -> None:
        with self._lock:
            self.receipts_issued += 1

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pg_calls": self.pg_calls,
                "pg_successes": self.pg_successes,
                "pg_failures": self.pg_failures,
                "fallback_calls": self.fallback_calls,
                "fallback_successes": self.fallback_successes,
                "fallback_failures": self.fallback_failures,
                "circuit_opens": self.circuit_opens,
                "reconciliations_run": self.reconciliations_run,
                "reconciliation_pushed": self.reconciliation_pushed,
                "reconciliation_pulled": self.reconciliation_pulled,
                "reconciliation_conflicts": self.reconciliation_conflicts,
                "reconciliation_skipped": self.reconciliation_skipped,
                "fail_closed_events": self.fail_closed_events,
                "operations_attempted": self.operations_attempted,
                "expired_pending": self.expired_pending,
                "rejected_rows": self.rejected_rows,
                "receipts_issued": self.receipts_issued,
            }


class FailoverBoundedLimitError(RuntimeError):
    """Raised when SQLite fallback would exceed a bounded hard limit.

    The failover store fail-closes (rejects the write) rather than letting
    SQLite grow into a shadow central authority.
    """


def _require_number(
    name: str,
    value: Any,
    *,
    minimum: float,
    exclusive: bool = False,
) -> float:
    if value is None or isinstance(value, bool):
        raise ReconciliationContractError(f"SQLITE_FALLBACK_BOUND_MISSING:{name}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ReconciliationContractError(
            f"SQLITE_FALLBACK_BOUND_INVALID:{name}:{value!r}"
        ) from error
    if not math.isfinite(number):
        raise ReconciliationContractError(
            f"SQLITE_FALLBACK_BOUND_UNBOUNDED:{name}:{value!r}"
        )
    if exclusive and number <= minimum:
        raise ReconciliationContractError(
            f"SQLITE_FALLBACK_BOUND_INVALID:{name}:{number}"
        )
    if not exclusive and number < minimum:
        raise ReconciliationContractError(
            f"SQLITE_FALLBACK_BOUND_INVALID:{name}:{number}"
        )
    return number


@dataclass(frozen=True)
class BoundedLimits:
    """Hard limits on the SQLite degraded buffer (A508, fail-closed).

    Defaults are the codex ``sql_reconciliation_contract`` values
    (``DEFAULT_SQLITE_TO_POSTGRESQL``).  Missing/null/unbounded values are
    rejected in ``__post_init__``; nothing here is optional.
    """

    max_pending_count: int = 10_000
    max_db_size_bytes: int = 256 * 1024 * 1024  # 256 MB
    max_wal_size_bytes: int = 64 * 1024 * 1024  # 64 MB
    max_degraded_seconds: float = 3600.0  # 1 hour
    reconcile_deadline_seconds: float = 300.0  # from registration
    max_staleness_seconds: float = 300.0  # pending row freshness
    max_operations: int = 50_000  # degraded-path operations
    expiry_action: str = "fail-closed-expire-and-retain-evidence"
    state_class: str = "BOUNDED_DEGRADED_BUFFER"
    contract_code: str = "DEFAULT_SQLITE_TO_POSTGRESQL"

    def __post_init__(self) -> None:
        for name, minimum, exclusive in (
            ("max_pending_count", 0.0, False),
            ("max_db_size_bytes", 0.0, False),
            ("max_wal_size_bytes", 0.0, False),
            ("max_degraded_seconds", 0.0, True),
            ("reconcile_deadline_seconds", 0.0, True),
            ("max_staleness_seconds", 0.0, True),
            ("max_operations", 0.0, False),
        ):
            _require_number(
                name,
                getattr(self, name),
                minimum=minimum,
                exclusive=exclusive,
            )
        if str(self.expiry_action) not in EXPIRY_ACTIONS:
            raise ReconciliationContractError(
                f"SQLITE_FALLBACK_EXPIRY_ACTION_INVALID:{self.expiry_action}"
            )
        if str(self.state_class) not in FALLBACK_STATE_CLASSES:
            raise ReconciliationContractError(
                f"SQLITE_FALLBACK_STATE_CLASS_INVALID:{self.state_class}"
            )
        if not str(self.contract_code).strip():
            raise ReconciliationContractError(
                "SQLITE_FALLBACK_CONTRACT_CODE_MISSING"
            )

    @classmethod
    def from_contract(
        cls, contract: SqliteReconciliationContract
    ) -> "BoundedLimits":
        return cls(
            max_pending_count=int(contract.maximum_buffer_rows),
            reconcile_deadline_seconds=float(
                contract.reconciliation_deadline_seconds
            ),
            max_staleness_seconds=float(contract.maximum_staleness_seconds),
            max_operations=int(contract.maximum_operations),
            expiry_action=contract.expiry_action,
            state_class=contract.state_class,
            contract_code=contract.contract_code,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_pending_count": self.max_pending_count,
            "max_db_size_bytes": self.max_db_size_bytes,
            "max_wal_size_bytes": self.max_wal_size_bytes,
            "max_degraded_seconds": self.max_degraded_seconds,
            "reconcile_deadline_seconds": self.reconcile_deadline_seconds,
            "max_staleness_seconds": self.max_staleness_seconds,
            "max_operations": self.max_operations,
            "expiry_action": self.expiry_action,
            "state_class": self.state_class,
            "contract_code": self.contract_code,
        }


class FailoverSharedLayerStore:
    """PostgreSQL ↔ SQLite degraded closure with bounded pending tracking.

    Parameters
    ----------
    primary_store
        The canonical ``PostgresSharedLayerStore`` (or any object exposing
        the same method surface).
    fallback_store
        The bounded local ``LocalSharedLayerStore`` used only when the
        primary is unavailable and policy permits failover.
    reconcile_connection
        SQLite connection used for the local ``reconcile_state`` table.
        Owned by the caller; the failover store only adds a table.
    policy_allows_failover
        Callable returning ``True`` when the current governance policy
        permits routing to the local fallback.  Defaults to always-True
        for tests; production wires the directory-authority snapshot.
    config
        Optional ``ResilientStoreConfig`` for the circuit breaker.
    bounded_limits
        Explicit module-private bounds.  Must not exceed the codex contract
        bounds; omitted values are taken from the codex contract.
    db_path
        Physical path of the fallback SQLite database.  When declared, an
        A512 ``sqlite_scope`` binding is required or fallback fails closed.
    contract / codex_db_path
        Pre-validated A508/A509 contract, or the codex database path to
        load it from.  Unavailable contract ⇒ failover disabled.
    generation_provider / generation_ledger
        A509 generation binding.  The provider is consulted for writes and
        reconciliation; the ledger (when supplied) fence-checks the
        ``reconcile`` workload.
    pg_identity_resolver / pg_commit_identity_provider
        Hooks for the assembly layer to prove the central PostgreSQL target
        identity and the commit identity used in receipts.
    permission_revalidator
        Hook ``(module_id, resource_id, context) -> bool`` used to
        revalidate permission before reconciliation.  Defaults to the
        failover policy gate.
    sqlite_scope
        A512 ``SqliteScopeBinding`` for the file-backed fallback database.
    """

    def __init__(
        self,
        primary_store: Any,
        fallback_store: Any,
        reconcile_connection: sqlite3.Connection,
        *,
        policy_allows_failover: Optional[Callable[[], bool]] = None,
        config: Optional[ResilientStoreConfig] = None,
        bounded_limits: Optional[BoundedLimits] = None,
        db_path: Optional[str] = None,
        contract: Optional[SqliteReconciliationContract] = None,
        codex_db_path: Optional[str] = None,
        generation_provider: Optional[Callable[[], Optional[int]]] = None,
        generation_ledger: Optional[GenerationLedger] = None,
        pg_identity_resolver: Optional[Callable[[Any], Optional[str]]] = None,
        pg_commit_identity_provider: Optional[
            Callable[[Any, dict[str, Any]], Optional[str]]
        ] = None,
        permission_revalidator: Optional[
            Callable[[str, str, dict[str, Any]], bool]
        ] = None,
        sqlite_scope: Optional[SqliteScopeBinding] = None,
        reconcile_service_factory: Optional[Callable[[Any, Any], Any]] = None,
    ) -> None:
        self._primary = primary_store
        self._fallback = fallback_store
        self._reconcile_conn = reconcile_connection
        self._reconcile_conn.row_factory = sqlite3.Row
        self._policy_allows_failover = policy_allows_failover or (lambda: True)
        self._config = config or ResilientStoreConfig()
        self._stats = FailoverStats()
        self._db_path = db_path
        self._degraded_since: Optional[float] = None  # monotonic timestamp
        self._reconcile_started_at: Optional[float] = None
        self._operation_count = 0
        self._reconcile_service_factory = reconcile_service_factory
        self._generation = 0
        self._generation_error: Optional[str] = None
        self._generation_provider = generation_provider
        self._generation_ledger = generation_ledger
        self._pg_identity_resolver = pg_identity_resolver
        self._pg_commit_identity_provider = pg_commit_identity_provider
        self._permission_revalidator = permission_revalidator
        self._sqlite_scope = sqlite_scope
        self._sqlite_scope_error: Optional[str] = None
        self._sqlite_scope_verified: Optional[bool] = None

        # --- A508/A509 contract binding (fail-closed) ------------------
        self._contract_error: Optional[str] = None
        if contract is not None:
            self._contract: Optional[SqliteReconciliationContract] = contract
        else:
            try:
                self._contract = load_reconciliation_contract(codex_db_path)
            except ReconciliationContractError as exc:
                self._contract = None
                self._contract_error = str(exc)
                _logger.error(
                    "failover_store: A509 reconciliation contract unavailable: %s",
                    exc,
                )
        if bounded_limits is not None:
            if self._contract is not None:
                self._contract.assert_covers(bounded_limits)
            self._bounded_limits: Optional[BoundedLimits] = bounded_limits
        elif self._contract is not None:
            self._bounded_limits = BoundedLimits.from_contract(self._contract)
        else:
            self._bounded_limits = None

        if generation_provider is not None:
            try:
                provided = generation_provider()
                if provided is not None:
                    self._generation = self._validate_generation(provided)
            except Exception as exc:  # noqa: BLE001 - fail closed
                self._generation_error = f"GENERATION_PROVIDER_FAILED:{exc}"
                self._generation = -1
        self._sqlite_scope_verified = self._assess_sqlite_scope()

        self._circuit = CircuitBreaker(
            name="failover-shared-layer",
            failure_threshold=self._config.circuit_failure_threshold,
            recovery_timeout=self._config.circuit_recovery_timeout,
            on_open=self._stats.record_circuit_open,
        )
        self._lock = threading.RLock()
        self._ensure_reconcile_schema()

    # ------------------------------------------------------------------
    # Reconcile state schema (local pending tracking)
    # ------------------------------------------------------------------

    _RECONCILE_COLUMNS: tuple[tuple[str, str], ...] = (
        ("correlation_id", "TEXT"),
        ("request_id", "TEXT"),
        ("decision_id", "TEXT"),
        ("registered_at", "TEXT"),
        ("reconcile_generation", "INTEGER"),
        ("receipt_json", "TEXT"),
    )

    def _ensure_reconcile_schema(self) -> None:
        with self._lock:
            self._reconcile_conn.execute(
                """CREATE TABLE IF NOT EXISTS reconcile_state (
                    module_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    local_version INTEGER NOT NULL,
                    local_updated_at TEXT NOT NULL,
                    local_content_hash TEXT,
                    idempotency_key TEXT,
                    reconcile_status TEXT NOT NULL DEFAULT 'pending',
                    reconciled_at TEXT,
                    correlation_id TEXT,
                    request_id TEXT,
                    decision_id TEXT,
                    PRIMARY KEY (module_id, resource_id)
                )"""
            )
            # Idempotent column additions for pre-existing tables.
            for column, column_type in self._RECONCILE_COLUMNS:
                try:
                    self._reconcile_conn.execute(
                        f"ALTER TABLE reconcile_state ADD COLUMN {column} {column_type}"
                    )
                except sqlite3.OperationalError:
                    pass
            self._reconcile_conn.execute(
                "CREATE INDEX IF NOT EXISTS reconcile_state_pending_idx "
                "ON reconcile_state (reconcile_status, local_updated_at) "
                "WHERE reconcile_status = 'pending'"
            )
            self._reconcile_conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS reconcile_state_idem_idx "
                "ON reconcile_state (idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            self._reconcile_conn.execute(
                "CREATE INDEX IF NOT EXISTS reconcile_state_correlation_idx "
                "ON reconcile_state (correlation_id) WHERE correlation_id IS NOT NULL"
            )
            # Local audit trail — never the central authority, but carries
            # the same correlation_id / request_id / decision_id so the
            # full execution chain can be stitched after PG recovery.
            self._reconcile_conn.execute(
                """CREATE TABLE IF NOT EXISTS local_audit_event (
                    event_id TEXT PRIMARY KEY,
                    correlation_id TEXT,
                    request_id TEXT,
                    decision_id TEXT,
                    actor TEXT,
                    module_id TEXT,
                    resource_id TEXT,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    details TEXT,
                    occurred_at TEXT NOT NULL
                )"""
            )
            self._reconcile_conn.execute(
                "CREATE INDEX IF NOT EXISTS local_audit_correlation_idx "
                "ON local_audit_event (correlation_id) WHERE correlation_id IS NOT NULL"
            )
            self._reconcile_conn.execute(
                "CREATE INDEX IF NOT EXISTS local_audit_request_idx "
                "ON local_audit_event (request_id) WHERE request_id IS NOT NULL"
            )
            self._reconcile_conn.commit()

    # ------------------------------------------------------------------
    # A512 SQLite scope boundary
    # ------------------------------------------------------------------

    def _assess_sqlite_scope(self) -> Optional[bool]:
        """Verify path allowlist + identity + locator + owner-only ACL.

        Returns ``True`` (verified), ``False`` (declared but unverifiable —
        fail closed) or ``None`` (no file-backed path declared; legacy
        in-memory test doubles only).
        """
        binding = self._sqlite_scope
        declared_path = ""
        if binding is not None:
            declared_path = str(binding.path or "").strip()
        target = declared_path or str(self._db_path or "").strip()
        if binding is None and not target:
            return None
        if binding is not None and declared_path and self._db_path:
            declared_abs = os.path.abspath(declared_path)
            actual_abs = os.path.abspath(str(self._db_path))
            if declared_abs != actual_abs:
                self._sqlite_scope_error = (
                    f"SQLITE_SCOPE_PATH_MISMATCH:{declared_abs}:{actual_abs}"
                )
                _logger.error(
                    "failover_store: A512 sqlite scope binding path mismatch: %s",
                    self._sqlite_scope_error,
                )
                return False
        if binding is None:
            self._sqlite_scope_error = "SQLITE_SCOPE_BINDING_MISSING"
            _logger.error(
                "failover_store: file-backed SQLite buffer %s declared without "
                "an A512 sqlite_scope binding; failover disabled",
                target,
            )
            return False
        if not target or target == ":memory:":
            self._sqlite_scope_error = "SQLITE_SCOPE_PATH_UNRESOLVED"
            return False
        try:
            assert_binding(binding, target)
        except SqliteScopeError as exc:
            self._sqlite_scope_error = str(exc)
            _logger.error(
                "failover_store: A512 sqlite scope verification failed: %s", exc
            )
            return False
        return True

    def _check_sqlite_scope(self) -> None:
        if self._sqlite_scope_verified is False:
            self._stats.record_fail_closed()
            raise SqliteScopeError(
                "SQLITE_SCOPE_UNVERIFIED:"
                f"{self._sqlite_scope_error or 'unknown'}"
            )

    @property
    def sqlite_scope_verified(self) -> Optional[bool]:
        return self._sqlite_scope_verified

    # ------------------------------------------------------------------
    # Pending tracking
    # ------------------------------------------------------------------

    def _validate_generation(self, generation: Any) -> int:
        if isinstance(generation, bool) or generation is None:
            raise ReconciliationContractError(
                f"RECONCILE_GENERATION_INVALID:{generation!r}"
            )
        if isinstance(generation, int):
            value = generation
        else:
            try:
                value = int(str(generation).strip())
            except (TypeError, ValueError) as error:
                raise ReconciliationContractError(
                    f"RECONCILE_GENERATION_INVALID:{generation!r}"
                ) from error
            if isinstance(generation, float) and float(generation) != value:
                raise ReconciliationContractError(
                    f"RECONCILE_GENERATION_INVALID:{generation!r}"
                )
        if value < 0:
            raise ReconciliationContractError(
                f"RECONCILE_GENERATION_INVALID:{value}"
            )
        return value

    def _current_generation(self) -> Optional[int]:
        if self._generation_provider is None:
            if self._generation < 0:
                return None
            return self._generation
        if self._generation_error is not None and self._generation < 0:
            raise ReconciliationContractError(self._generation_error)
        try:
            provided = self._generation_provider()
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise ReconciliationContractError(
                f"RECONCILE_GENERATION_PROVIDER_FAILED:{exc}"
            ) from exc
        if provided is None:
            return None
        value = self._validate_generation(provided)
        self._generation = value
        return value

    def mark_pending(
        self,
        module_id: str,
        resource_id: str,
        version: int,
        updated_at: str,
        content_hash: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        correlation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        generation: Optional[int] = None,
        registered_at: Optional[str] = None,
    ) -> None:
        """Record a bounded pending change from a fallback write.

        ``registered_at`` is preserved from the first registration; the
        reconciliation deadline is measured from it (A509).  ``generation``
        defaults to the store's bound generation.
        """
        self._check_sqlite_scope()
        if generation is not None:
            resolved_generation = self._validate_generation(generation)
        else:
            current = self._current_generation()
            if current is None:
                raise ReconciliationContractError("RECONCILE_GENERATION_UNBOUND")
            resolved_generation = current
        registration = str(registered_at or "").strip() or _now_iso()
        with self._lock:
            self._reconcile_conn.execute(
                """INSERT INTO reconcile_state
                    (module_id, resource_id, local_version, local_updated_at,
                     local_content_hash, idempotency_key, reconcile_status,
                     correlation_id, request_id, decision_id, registered_at,
                     reconcile_generation)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                   ON CONFLICT(module_id, resource_id) DO UPDATE SET
                     local_version = excluded.local_version,
                     local_updated_at = excluded.local_updated_at,
                     local_content_hash = excluded.local_content_hash,
                     idempotency_key = excluded.idempotency_key,
                     reconcile_status = 'pending',
                     reconciled_at = NULL,
                     correlation_id = excluded.correlation_id,
                     request_id = excluded.request_id,
                     decision_id = excluded.decision_id,
                     registered_at = COALESCE(reconcile_state.registered_at,
                                              excluded.registered_at),
                     reconcile_generation = excluded.reconcile_generation,
                     receipt_json = NULL""",
                (module_id, resource_id, version, updated_at, content_hash,
                 idempotency_key, correlation_id, request_id, decision_id,
                 registration, resolved_generation),
            )
            self._reconcile_conn.commit()

    def record_local_audit(
        self,
        *,
        action: str,
        outcome: str,
        actor: Optional[str] = None,
        module_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> str:
        """Record a local audit event with correlation IDs.

        Local SQLite audit is never the central authority, but carries
        the same correlation_id / request_id / decision_id so the full
        execution chain can be stitched after PG recovery.
        """
        self._check_sqlite_scope()
        return self._insert_local_audit(
            action=action,
            outcome=outcome,
            actor=actor,
            module_id=module_id,
            resource_id=resource_id,
            correlation_id=correlation_id,
            request_id=request_id,
            decision_id=decision_id,
            details=details,
        )

    def _insert_local_audit(
        self,
        *,
        action: str,
        outcome: str,
        actor: Optional[str] = None,
        module_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> str:
        """Internal evidence append (callers enforce the scope boundary)."""
        event_id = str(uuid.uuid4())
        occurred_at = _now_iso()
        details_json = json.dumps(details or {}, ensure_ascii=False)
        with self._lock:
            self._reconcile_conn.execute(
                """INSERT INTO local_audit_event
                    (event_id, correlation_id, request_id, decision_id,
                     actor, module_id, resource_id, action, outcome,
                     details, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, correlation_id, request_id, decision_id,
                 actor, module_id, resource_id, action, outcome,
                 details_json, occurred_at),
            )
            self._reconcile_conn.commit()
        return event_id

    def local_audit_events(
        self,
        *,
        correlation_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Retrieve local audit events by correlation or request ID."""
        columns = (
            "event_id, correlation_id, request_id, decision_id, actor, "
            "module_id, resource_id, action, outcome, details, occurred_at"
        )
        with self._lock:
            if correlation_id:
                rows = self._reconcile_conn.execute(
                    f"SELECT {columns} FROM local_audit_event WHERE correlation_id = ? "
                    "ORDER BY occurred_at ASC",
                    (correlation_id,),
                ).fetchall()
            elif request_id:
                rows = self._reconcile_conn.execute(
                    f"SELECT {columns} FROM local_audit_event WHERE request_id = ? "
                    "ORDER BY occurred_at ASC",
                    (request_id,),
                ).fetchall()
            else:
                rows = self._reconcile_conn.execute(
                    f"SELECT {columns} FROM local_audit_event ORDER BY occurred_at ASC LIMIT 100"
                ).fetchall()
        return [dict(row) for row in rows]

    def pending_count(self, module_id: Optional[str] = None) -> int:
        with self._lock:
            if module_id:
                row = self._reconcile_conn.execute(
                    "SELECT COUNT(*) FROM reconcile_state "
                    "WHERE module_id = ? AND reconcile_status = 'pending'",
                    (module_id,),
                ).fetchone()
            else:
                row = self._reconcile_conn.execute(
                    "SELECT COUNT(*) FROM reconcile_state WHERE reconcile_status = 'pending'"
                ).fetchone()
        return int(row[0]) if row else 0

    def _mark_reconciled(
        self,
        module_id: str,
        resource_id: str,
        status: str,
        timestamp: str,
    ) -> None:
        if status not in {"in-sync", "conflict", "expired", "rejected"}:
            raise ValueError(f"invalid reconcile status: {status}")
        with self._lock:
            self._reconcile_conn.execute(
                """UPDATE reconcile_state SET
                     reconcile_status = ?, reconciled_at = ?
                   WHERE module_id = ? AND resource_id = ?""",
                (status, timestamp, module_id, resource_id),
            )
            self._reconcile_conn.commit()

    def _store_receipt(
        self,
        module_id: str,
        resource_id: str,
        receipt: ReconciliationReceipt,
    ) -> None:
        with self._lock:
            self._reconcile_conn.execute(
                """UPDATE reconcile_state SET receipt_json = ?
                   WHERE module_id = ? AND resource_id = ?""",
                (receipt.canonical_json(), module_id, resource_id),
            )
            self._reconcile_conn.commit()

    def reconciliation_receipts(
        self,
        module_id: Optional[str] = None,
    ) -> list[ReconciliationReceipt]:
        """Read persisted typed reconciliation receipts (A509)."""
        with self._lock:
            if module_id:
                rows = self._reconcile_conn.execute(
                    "SELECT receipt_json FROM reconcile_state "
                    "WHERE receipt_json IS NOT NULL AND module_id = ?",
                    (module_id,),
                ).fetchall()
            else:
                rows = self._reconcile_conn.execute(
                    "SELECT receipt_json FROM reconcile_state "
                    "WHERE receipt_json IS NOT NULL"
                ).fetchall()
        receipts: list[ReconciliationReceipt] = []
        for row in rows:
            try:
                payload = json.loads(row["receipt_json"])
            except (TypeError, ValueError):
                continue
            receipts.append(ReconciliationReceipt(**{
                key: payload[key] for key in ReconciliationReceipt.__dataclass_fields__
                if key in payload
            }))
        return receipts

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def _idempotency_seen(self, idempotency_key: Optional[str]) -> bool:
        """Return True if this idempotency key was already accepted."""
        if not idempotency_key:
            return False
        with self._lock:
            row = self._reconcile_conn.execute(
                "SELECT 1 FROM reconcile_state WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Failover routing
    # ------------------------------------------------------------------

    @property
    def circuit_state(self) -> CircuitState:
        return self._circuit.state

    @property
    def stats(self) -> FailoverStats:
        return self._stats

    @property
    def contract(self) -> Optional[SqliteReconciliationContract]:
        return self._contract

    def _failover_permitted(self) -> bool:
        if self._contract is None or self._bounded_limits is None:
            return False
        if self._sqlite_scope_verified is False:
            return False
        try:
            return bool(self._policy_allows_failover())
        except Exception:
            return False

    def _expire_stale_pending(self) -> int:
        """A508 expiry: mark stale rows, retain evidence, never delete."""
        limits = self._bounded_limits
        if limits is None:
            return 0
        now = time.time()
        with self._lock:
            rows = self._reconcile_conn.execute(
                """SELECT module_id, resource_id, local_version,
                          local_updated_at, local_content_hash,
                          idempotency_key, registered_at,
                          reconcile_generation
                   FROM reconcile_state
                   WHERE reconcile_status = 'pending'"""
            ).fetchall()
            stale: list[tuple[sqlite3.Row, str]] = []
            for row in rows:
                updated = _parse_timestamp(row["local_updated_at"])
                registered = _parse_timestamp(row["registered_at"])
                reasons: list[str] = []
                if updated is None:
                    reasons.append("unparseable-updated-at")
                elif now - updated >= limits.max_staleness_seconds:
                    reasons.append("max-staleness")
                if registered is None:
                    reasons.append("registration-unbound")
                elif now - registered >= limits.reconcile_deadline_seconds:
                    reasons.append("reconciliation-deadline")
                if reasons:
                    stale.append((row, ",".join(reasons)))
            if not stale:
                return 0
            timestamp = _now_iso()
            for row, reason in stale:
                module_id = str(row["module_id"])
                resource_id = str(row["resource_id"])
                self._reconcile_conn.execute(
                    """UPDATE reconcile_state SET
                         reconcile_status = 'expired', reconciled_at = ?
                       WHERE module_id = ? AND resource_id = ?""",
                    (timestamp, module_id, resource_id),
                )
                self._insert_local_audit(
                    action="expire_pending",
                    outcome=limits.expiry_action,
                    module_id=module_id,
                    resource_id=resource_id,
                    details={
                        "reason": reason,
                        "local_version": int(row["local_version"]),
                        "local_content_hash": row["local_content_hash"],
                        "reconcile_generation": row["reconcile_generation"],
                        "max_staleness_seconds": limits.max_staleness_seconds,
                        "reconcile_deadline_seconds":
                            limits.reconcile_deadline_seconds,
                    },
                )
            self._reconcile_conn.commit()
            self._stats.record_expired(len(stale))
            return len(stale)

    def _check_bounded_limits(self) -> None:
        """Fail-closed if any SQLite bounded hard limit is exceeded (A508)."""
        limits = self._bounded_limits
        if limits is None:
            self._stats.record_fail_closed()
            raise FailoverBoundedLimitError(
                "bounded limits unconfigured: "
                f"{self._contract_error or 'no A509 contract'}"
            )
        # 1. Max pending count
        pending = self.pending_count()
        if pending >= limits.max_pending_count:
            self._stats.record_fail_closed()
            raise FailoverBoundedLimitError(
                f"max_pending_count exceeded: {pending} >= {limits.max_pending_count}"
            )
        # 2. Max degraded operations
        if self._operation_count >= limits.max_operations:
            self._stats.record_fail_closed()
            raise FailoverBoundedLimitError(
                f"max_operations exceeded: {self._operation_count} >= {limits.max_operations}"
            )
        # 3. Staleness / registration deadline → expire and retain evidence.
        expired = self._expire_stale_pending()
        if expired:
            self._stats.record_fail_closed()
            raise FailoverBoundedLimitError(
                f"stale-pending-expired:{expired}:{limits.expiry_action}"
            )
        # 4. Max DB size
        if self._db_path:
            try:
                db_size = os.path.getsize(self._db_path)
                if db_size >= limits.max_db_size_bytes:
                    self._stats.record_fail_closed()
                    raise FailoverBoundedLimitError(
                        f"max_db_size_bytes exceeded: {db_size} >= {limits.max_db_size_bytes}"
                    )
                wal_path = self._db_path + "-wal"
                try:
                    wal_size = os.path.getsize(wal_path)
                    if wal_size >= limits.max_wal_size_bytes:
                        self._stats.record_fail_closed()
                        raise FailoverBoundedLimitError(
                            f"max_wal_size_bytes exceeded: {wal_size} >= {limits.max_wal_size_bytes}"
                        )
                except OSError:
                    pass
            except OSError:
                pass
        # 5. Max degraded time
        if self._degraded_since is not None:
            degraded_elapsed = time.monotonic() - self._degraded_since
            if degraded_elapsed >= limits.max_degraded_seconds:
                self._stats.record_fail_closed()
                raise FailoverBoundedLimitError(
                    f"max_degraded_seconds exceeded: {degraded_elapsed:.1f} >= {limits.max_degraded_seconds}"
                )
        # 6. Reconcile deadline (registration-based rows handled by expiry;
        #    this retained timestamp check bounds a dead reconcile worker).
        if self._reconcile_started_at is not None:
            reconcile_elapsed = time.monotonic() - self._reconcile_started_at
            if reconcile_elapsed >= limits.reconcile_deadline_seconds:
                self._stats.record_fail_closed()
                raise FailoverBoundedLimitError(
                    f"reconcile_deadline_seconds exceeded: {reconcile_elapsed:.1f} >= {limits.reconcile_deadline_seconds}"
                )

    def _route(self, operation: str, fn_primary: Callable[..., Any], fn_fallback: Callable[..., Any], *args: Any) -> Any:
        """Route an operation to primary or fallback with circuit breaker."""
        # If circuit is open and no recovery timeout elapsed, go straight to fallback.
        if self._circuit.state == CircuitState.OPEN:
            # A512 boundary violations surface explicitly before routing.
            self._check_sqlite_scope()
            if self._failover_permitted():
                return self._call_fallback(operation, fn_fallback, *args)
            raise CircuitOpenError(f"circuit open and failover not permitted for {operation}")

        # Try primary
        try:
            result = self._circuit.call(fn_primary, *args)
            self._stats.record_pg_success()
            return result
        except CircuitOpenError:
            # Circuit opened during this call
            self._check_sqlite_scope()
            if self._failover_permitted():
                return self._call_fallback(operation, fn_fallback, *args)
            raise
        except Exception as exc:
            self._stats.record_pg_failure()
            _logger.warning("failover_store: primary %s failed: %s", operation, exc)
            # A512: an unverifiable SQLite boundary must not fall back.
            self._check_sqlite_scope()
            if self._failover_permitted():
                return self._call_fallback(operation, fn_fallback, *args)
            raise

    def _call_fallback(self, operation: str, fn: Callable[..., Any], *args: Any) -> Any:
        # A512/A508 fail-closed gates before any SQLite write.
        self._check_sqlite_scope()
        self._check_bounded_limits()
        self._operation_count += 1
        self._stats.record_operation()
        # Track degraded start time on first fallback.
        if self._degraded_since is None:
            self._degraded_since = time.monotonic()
        try:
            result = fn(*args)
            self._stats.record_fallback_success()
            return result
        except Exception as exc:
            self._stats.record_fallback_failure()
            _logger.error("failover_store: fallback %s failed: %s", operation, exc)
            raise

    # ------------------------------------------------------------------
    # Store method surface (delegated with failover)
    # ------------------------------------------------------------------

    def submit_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        payload: Any,
    ) -> None:
        # Extract idempotency key from payload if present
        idem_key: Optional[str] = None
        if isinstance(payload, dict):
            idem_key = str(payload.get("idempotency_key") or "") or None

        if idem_key and self._idempotency_seen(idem_key):
            _logger.info("failover_store: idempotent skip for key=%s", idem_key)
            return  # already accepted

        def _primary() -> None:
            self._primary.submit_request(token, request_id, target_tool_id, payload)

        def _fallback() -> None:
            self._fallback.submit_request(token, request_id, target_tool_id, payload)
            # Mark pending for later reconciliation
            module_id = "shared-layer"
            resource_id = request_id
            content_hash = None
            correlation_id = None
            decision_id = None
            if isinstance(payload, dict):
                content_hash = str(payload.get("content_hash") or "") or None
                module_id = str(payload.get("module_id") or "shared-layer")
                correlation_id = str(payload.get("correlation_id") or "") or None
                decision_id = str(payload.get("decision_id") or "") or None
            self.mark_pending(
                module_id=module_id,
                resource_id=resource_id,
                version=1,
                updated_at=_now_iso(),
                content_hash=content_hash,
                idempotency_key=idem_key,
                correlation_id=correlation_id,
                request_id=request_id,
                decision_id=decision_id,
            )
            # Record local audit event with correlation chain.
            self.record_local_audit(
                action="submit_request_fallback",
                outcome="degraded-pending",
                module_id=module_id,
                resource_id=resource_id,
                correlation_id=correlation_id,
                request_id=request_id,
                decision_id=decision_id,
                details={"target_tool_id": target_tool_id},
            )

        self._route("submit_request", _primary, _fallback, )

    def cancel_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> bool:
        return self._route(
            "cancel_request",
            lambda: self._primary.cancel_request(token, request_id, target_tool_id),
            lambda: self._fallback.cancel_request(token, request_id, target_tool_id),
        )

    def request_cancelled(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> bool:
        return self._route(
            "request_cancelled",
            lambda: self._primary.request_cancelled(token, request_id, target_tool_id),
            lambda: self._fallback.request_cancelled(token, request_id, target_tool_id),
        )

    def consume_response(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        return self._route(
            "consume_response",
            lambda: self._primary.consume_response(token, request_id, target_tool_id),
            lambda: self._fallback.consume_response(token, request_id, target_tool_id),
        )

    def publish_progress(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        progress: Any,
    ) -> bool:
        return self._route(
            "publish_progress",
            lambda: self._primary.publish_progress(token, request_id, target_tool_id, progress),
            lambda: self._fallback.publish_progress(token, request_id, target_tool_id, progress),
        )

    def claim_request(
        self,
        token: str,
        target_tool_id: str,
        *,
        lease_duration_seconds: float = 300.0,
    ) -> dict[str, Any] | None:
        return self._route(
            "claim_request",
            lambda: self._primary.claim_request(token, target_tool_id, lease_duration_seconds=lease_duration_seconds),
            lambda: self._fallback.claim_request(token, target_tool_id, lease_duration_seconds=lease_duration_seconds),
        )

    def reclaim_expired(
        self,
        token: str,
        target_tool_id: str,
    ) -> int:
        return self._route(
            "reclaim_expired",
            lambda: self._primary.reclaim_expired(token, target_tool_id),
            lambda: self._fallback.reclaim_expired(token, target_tool_id),
        )

    def respond(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        response: Any,
    ) -> bool:
        return self._route(
            "respond",
            lambda: self._primary.respond(token, request_id, target_tool_id, response),
            lambda: self._fallback.respond(token, request_id, target_tool_id, response),
        )

    def notify_channel(self, token: str, target_tool_id: str) -> None:
        self._route(
            "notify_channel",
            lambda: self._primary.notify_channel(token, target_tool_id),
            lambda: self._fallback.notify_channel(token, target_tool_id),
        )

    # ------------------------------------------------------------------
    # Reconciliation (one-directional SQLite → PostgreSQL)
    # ------------------------------------------------------------------

    def _verify_pg_target(
        self,
        pg_connection: Any,
        contract: SqliteReconciliationContract,
    ) -> Optional[str]:
        """A509: bind reconciliation to the registered central PG identity."""
        if pg_connection is None:
            return None
        purpose = getattr(pg_connection, "dsn_purpose", None)
        if isinstance(purpose, str):
            normalized = purpose.strip().casefold()
            if normalized in {"admin", "backup"}:
                raise ReconciliationContractError(
                    f"PG_TARGET_ELEVATED_PURPOSE_FORBIDDEN:{purpose}"
                )
        resolver = self._pg_identity_resolver or _default_pg_identity_resolver
        try:
            identity = resolver(pg_connection)
        except ReconciliationContractError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise ReconciliationContractError(
                f"PG_TARGET_IDENTITY_RESOLUTION_FAILED:{exc}"
            ) from exc
        if not isinstance(identity, str) or not identity.strip():
            raise ReconciliationContractError("PG_TARGET_IDENTITY_UNVERIFIED")
        identity = identity.strip()
        if identity != contract.target_postgresql_identity:
            raise ReconciliationContractError(
                "PG_TARGET_IDENTITY_MISMATCH:"
                f"{identity}:{contract.target_postgresql_identity}"
            )
        return identity

    def _resolve_reconcile_generation(
        self,
        generation: Optional[int],
    ) -> int:
        if generation is not None:
            effective = self._validate_generation(generation)
        else:
            current = self._current_generation()
            if current is None:
                raise ReconciliationContractError("RECONCILE_GENERATION_UNBOUND")
            effective = current
        if self._generation_ledger is not None:
            try:
                assert_generation_current(effective, "reconcile", self._generation_ledger)
            except SecurityGenerationError as exc:
                raise ReconciliationContractError(
                    f"RECONCILE_GENERATION_REJECTED:{exc}"
                ) from exc
        return effective

    def _default_permission_revalidator(
        self,
        module_id: str,
        resource_id: str,
        context: dict[str, Any],
    ) -> bool:
        """Fallback revalidation gate — the failover policy decision.

        Production must wire the permission-sovereign revalidator; this
        default still fails closed when the policy gate is closed.
        """
        return self._failover_permitted()

    def _permission_decision_hash(
        self,
        module_id: str,
        resource_id: str,
        generation: int,
        allowed: bool,
    ) -> str:
        return _sha256(
            f"permission|{module_id}|{resource_id}|{generation}|{int(bool(allowed))}"
        )

    def _resolve_commit_identity(
        self,
        pg_connection: Any,
        target_identity: str,
        module_id: str,
        resource_id: str,
    ) -> str:
        if self._pg_commit_identity_provider is not None:
            value = self._pg_commit_identity_provider(
                pg_connection,
                {
                    "target_postgresql_identity": target_identity,
                    "module_id": module_id,
                    "resource_id": resource_id,
                },
            )
            if not isinstance(value, str) or not value.strip():
                raise ReconciliationContractError("PG_COMMIT_IDENTITY_UNVERIFIED")
            return value.strip()
        for attribute in ("commit_identity", "last_commit_identity"):
            value = getattr(pg_connection, attribute, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return f"{target_identity}/{module_id}/{resource_id}"

    def _issue_receipt(
        self,
        *,
        result: Any,
        contract: SqliteReconciliationContract,
        target_identity: Optional[str],
        generation: int,
        permission_hashes: dict[tuple[str, str], str],
        pg_connection: Any,
    ) -> Optional[ReconciliationReceipt]:
        action = str(getattr(result, "action", ""))
        if action not in {"pushed", "pulled", "in-sync", "conflict"}:
            return None
        if target_identity is None:
            return None
        module_id = str(getattr(result, "module_id", ""))
        resource_id = str(getattr(result, "resource_id", ""))
        permission_hash = permission_hashes.get((module_id, resource_id)) or _sha256(
            f"unrevalidated|{module_id}|{resource_id}|{generation}"
        )
        reconciled_at = _now_iso()
        body = {
            "local_record_id": f"{module_id}:{resource_id}",
            "target_postgresql_identity": target_identity,
            "validated_schema_hash": contract.contract_hash,
            "permission_decision_hash": permission_hash,
            "conflict_result": contract.conflict_policy,
            "postgresql_commit_identity": self._resolve_commit_identity(
                pg_connection, target_identity, module_id, resource_id
            ),
            "reconciled_at": reconciled_at,
            "expiry_cleanup_status": (
                "retained-for-evidence" if action == "conflict" else "complete"
            ),
        }
        row = self._reconcile_conn.execute(
            "SELECT local_content_hash FROM reconcile_state "
            "WHERE module_id = ? AND resource_id = ?",
            (module_id, resource_id),
        ).fetchone()
        content_hash = str(row["local_content_hash"] or "") if row else ""
        evidence_hash = _sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False)
            + f"|{content_hash}|{generation}"
        )
        receipt = ReconciliationReceipt(
            receipt_id=str(uuid.uuid4()),
            evidence_hash=evidence_hash,
            **body,
        )
        self._store_receipt(module_id, resource_id, receipt)
        self._stats.record_receipt()
        return receipt

    def reconcile(
        self,
        pg_connection: Any,
        *,
        batch_size: int = 100,
        generation: Optional[int] = None,
        permission_revalidator: Optional[
            Callable[[str, str, dict[str, Any]], bool]
        ] = None,
    ) -> dict[str, Any]:
        """Run one-directional SQLite → PostgreSQL reconciliation (A509).

        Requires a verified central PostgreSQL target identity, a matching
        generation bound to every pending row and (per contract) permission
        revalidation.  Conflict rows are flagged, never auto-resolved.
        Expired rows are marked ``expired`` and their evidence is retained.
        Each processed row yields an idempotent typed receipt stored with
        the row and returned under ``summary["receipts"]``.
        """
        self._check_sqlite_scope()
        contract = self._contract
        if contract is None:
            raise ReconciliationContractError(
                "SQL_RECONCILIATION_CONTRACT_UNAVAILABLE:"
                f"{self._contract_error or 'not-configured'}"
            )
        target_identity = self._verify_pg_target(pg_connection, contract)
        effective_generation = self._resolve_reconcile_generation(generation)
        revalidator = permission_revalidator or self._permission_revalidator
        if revalidator is None:
            revalidator = self._default_permission_revalidator

        with self._lock:
            self._stats.reconciliations_run += 1
            if self._reconcile_started_at is None:
                self._reconcile_started_at = time.monotonic()
            summary: dict[str, Any] = {
                "pushed": 0,
                "pulled": 0,
                "in-sync": 0,
                "conflict": 0,
                "skipped": 0,
                "expired": 0,
                "rejected": 0,
                "receipts": [],
            }
            # A508/A509: registration-based expiry before any central write.
            summary["expired"] = self._expire_stale_pending()
            pending_rows = self._reconcile_conn.execute(
                """SELECT module_id, resource_id, idempotency_key,
                          reconcile_generation
                   FROM reconcile_state
                   WHERE reconcile_status = 'pending'"""
            ).fetchall()
            # Generation fence: an unbound/stale row is never pushed.
            for row in pending_rows:
                row_generation = row["reconcile_generation"]
                if row_generation is not None and int(row_generation) != effective_generation:
                    module_id = str(row["module_id"])
                    resource_id = str(row["resource_id"])
                    self.record_local_audit(
                        action="reconcile_rejected",
                        outcome="generation-mismatch",
                        module_id=module_id,
                        resource_id=resource_id,
                        request_id=row["idempotency_key"],
                        details={
                            "row_generation": int(row_generation),
                            "reconcile_generation": effective_generation,
                        },
                    )
                    raise ReconciliationContractError(
                        "RECONCILE_GENERATION_MISMATCH:"
                        f"{module_id}:{resource_id}:"
                        f"{row_generation}:{effective_generation}"
                    )
            self._reconcile_conn.execute(
                """UPDATE reconcile_state SET reconcile_generation = ?
                   WHERE reconcile_status = 'pending'
                     AND reconcile_generation IS NULL""",
                (effective_generation,),
            )
            self._reconcile_conn.commit()

            # Permission revalidation pre-pass (A509): denied rows are
            # rejected with evidence and never reach the central push.
            permission_hashes: dict[tuple[str, str], str] = {}
            for row in pending_rows:
                module_id = str(row["module_id"])
                resource_id = str(row["resource_id"])
                context = {
                    "contract_code": contract.contract_code,
                    "target_postgresql_identity": contract.target_postgresql_identity,
                    "generation": effective_generation,
                    "idempotency_key": row["idempotency_key"],
                }
                try:
                    allowed = bool(revalidator(module_id, resource_id, context))
                except ReconciliationContractError:
                    raise
                except Exception as exc:  # noqa: BLE001 - fail closed
                    self.record_local_audit(
                        action="reconcile_rejected",
                        outcome="permission-revalidation-error",
                        module_id=module_id,
                        resource_id=resource_id,
                        request_id=row["idempotency_key"],
                        details={"error": str(exc)},
                    )
                    raise ReconciliationContractError(
                        f"PERMISSION_REVALIDATION_FAILED:{module_id}:{resource_id}:{exc}"
                    ) from exc
                decision_hash = self._permission_decision_hash(
                    module_id, resource_id, effective_generation, allowed
                )
                if not allowed:
                    self._mark_reconciled(
                        module_id, resource_id, "rejected", _now_iso()
                    )
                    self.record_local_audit(
                        action="reconcile_rejected",
                        outcome="permission-revalidation-denied",
                        module_id=module_id,
                        resource_id=resource_id,
                        request_id=row["idempotency_key"],
                        details={"permission_decision_hash": decision_hash},
                    )
                    self._stats.record_rejected()
                    summary["rejected"] += 1
                    continue
                permission_hashes[(module_id, resource_id)] = decision_hash

            factory = (
                self._reconcile_service_factory or _RECONCILE_SERVICE_FACTORY
            )
            if factory is None:
                raise ReconciliationContractError(
                    "RECONCILE_SERVICE_UNREGISTERED:"
                    "decision-owner factory not registered"
                )
            service = factory(self._reconcile_conn, pg_connection)
            # Group pending by module_id
            modules = self._reconcile_conn.execute(
                "SELECT DISTINCT module_id FROM reconcile_state WHERE reconcile_status = 'pending'"
            ).fetchall()
            for mod_row in modules:
                module_id = str(mod_row["module_id"])
                for result in service.reconcile_module(module_id, batch_size=batch_size):
                    action = str(result.action)
                    summary[action] = summary.get(action, 0) + 1
                    if action == "pushed":
                        self._stats.reconciliation_pushed += 1
                    elif action == "pulled":
                        self._stats.reconciliation_pulled += 1
                    elif action == "conflict":
                        self._stats.reconciliation_conflicts += 1
                    elif action == "skipped":
                        self._stats.reconciliation_skipped += 1
                    receipt = self._issue_receipt(
                        result=result,
                        contract=contract,
                        target_identity=target_identity,
                        generation=effective_generation,
                        permission_hashes=permission_hashes,
                        pg_connection=pg_connection,
                    )
                    if receipt is not None:
                        summary["receipts"].append(receipt)
            # Clear degraded tracking when pending reaches zero.
            if self.pending_count() == 0:
                self._degraded_since = None
                self._reconcile_started_at = None
            return summary

    def status(self) -> dict[str, Any]:
        degraded_elapsed = None
        if self._degraded_since is not None:
            degraded_elapsed = time.monotonic() - self._degraded_since
        reconcile_elapsed = None
        if self._reconcile_started_at is not None:
            reconcile_elapsed = time.monotonic() - self._reconcile_started_at
        return {
            "circuit": self._circuit.stats(),
            "stats": self._stats.as_dict(),
            "pending": self.pending_count(),
            "policy_allows_failover": self._failover_permitted(),
            "bounded_limits": (
                self._bounded_limits.as_dict()
                if self._bounded_limits is not None
                else None
            ),
            "operations_attempted": self._operation_count,
            "contract": (
                self._contract.as_dict() if self._contract is not None else None
            ),
            "contract_error": self._contract_error,
            "generation": self._generation if self._generation >= 0 else None,
            "generation_error": self._generation_error,
            "sqlite_scope_verified": self._sqlite_scope_verified,
            "sqlite_scope_error": self._sqlite_scope_error,
            "degraded_elapsed_seconds": degraded_elapsed,
            "reconcile_elapsed_seconds": reconcile_elapsed,
        }

    def reset_circuit(self) -> None:
        self._circuit.reset()


__all__ = [
    "FailoverSharedLayerStore",
    "FailoverStats",
    "FailoverBoundedLimitError",
    "BoundedLimits",
    "ReconciliationContractError",
    "ReconciliationReceipt",
    "SqliteReconciliationContract",
    "SqliteScopeBinding",
    "register_reconcile_service_factory",
]
