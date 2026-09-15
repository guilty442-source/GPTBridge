"""Failover shared layer store — PostgreSQL ↔ SQLite degraded closure.

Policy-gated failover wrapper around the canonical PostgreSQL
``PostgresSharedLayerStore`` and the bounded local fallback
``LocalSharedLayerStore``.

Closure rules (A8/E21 + A44/E30):

1. PostgreSQL healthy → route to PostgreSQL (canonical authority).
2. PostgreSQL failure (circuit open or write exception) AND policy
   permits → route to ``LocalSharedLayerStore``.  Each fallback write
   accumulates a bounded pending change in a local ``reconcile_state``
   table; SQLite never becomes the canonical authority.
3. PostgreSQL recovers → ``reconcile()`` invokes the one-directional
   ``ReconcileService`` (SQLite → PostgreSQL).  Pending rows are cleared
   only after reconciliation succeeds.
4. Conflicts (same version, different hash) are flagged as ``conflict``
   and left for an authorized owner to resolve.  The failover store
   never decides a conflict winner.

Idempotency: ``submit_request`` accepts an optional ``idempotency_key``
in the payload.  Duplicate submits with the same key are ignored by the
local fallback, preventing duplicate requests during failover flapping.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .resilient_circuit import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ResilientStoreConfig,
)

_logger = logging.getLogger("gptbridge.failover_store")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            }


class FailoverBoundedLimitError(RuntimeError):
    """Raised when SQLite fallback would exceed a bounded hard limit.

    The failover store fail-closes (rejects the write) rather than letting
    SQLite grow into a shadow central authority.
    """


@dataclass(frozen=True)
class BoundedLimits:
    """Hard limits on the SQLite degraded buffer (fail-closed).

    When any limit is exceeded the failover store rejects the fallback
    write and raises ``FailoverBoundedLimitError``.  SQLite never becomes
    an unbounded shadow central authority.
    """

    max_pending_count: int = 10_000
    max_db_size_bytes: int = 256 * 1024 * 1024  # 256 MB
    max_wal_size_bytes: int = 64 * 1024 * 1024  # 64 MB
    max_degraded_seconds: float = 3600.0  # 1 hour
    reconcile_deadline_seconds: float = 7200.0  # 2 hours after recovery


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
    ) -> None:
        self._primary = primary_store
        self._fallback = fallback_store
        self._reconcile_conn = reconcile_connection
        self._reconcile_conn.row_factory = sqlite3.Row
        self._policy_allows_failover = policy_allows_failover or (lambda: True)
        self._config = config or ResilientStoreConfig()
        self._stats = FailoverStats()
        self._bounded_limits = bounded_limits or BoundedLimits()
        self._db_path = db_path
        self._degraded_since: Optional[float] = None  # monotonic timestamp
        self._reconcile_started_at: Optional[float] = None
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
            for col in ("correlation_id", "request_id", "decision_id"):
                try:
                    self._reconcile_conn.execute(
                        f"ALTER TABLE reconcile_state ADD COLUMN {col} TEXT"
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
    # Pending tracking
    # ------------------------------------------------------------------

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
    ) -> None:
        """Record a bounded pending change from a fallback write."""
        with self._lock:
            self._reconcile_conn.execute(
                """INSERT INTO reconcile_state
                    (module_id, resource_id, local_version, local_updated_at,
                     local_content_hash, idempotency_key, reconcile_status,
                     correlation_id, request_id, decision_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                   ON CONFLICT(module_id, resource_id) DO UPDATE SET
                     local_version = excluded.local_version,
                     local_updated_at = excluded.local_updated_at,
                     local_content_hash = excluded.local_content_hash,
                     idempotency_key = excluded.idempotency_key,
                     reconcile_status = 'pending',
                     reconciled_at = NULL,
                     correlation_id = excluded.correlation_id,
                     request_id = excluded.request_id,
                     decision_id = excluded.decision_id""",
                (module_id, resource_id, version, updated_at, content_hash,
                 idempotency_key, correlation_id, request_id, decision_id),
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
        import uuid as _uuid
        event_id = str(_uuid.uuid4())
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
        with self._lock:
            if correlation_id:
                rows = self._reconcile_conn.execute(
                    "SELECT * FROM local_audit_event WHERE correlation_id = ? "
                    "ORDER BY occurred_at ASC",
                    (correlation_id,),
                ).fetchall()
            elif request_id:
                rows = self._reconcile_conn.execute(
                    "SELECT * FROM local_audit_event WHERE request_id = ? "
                    "ORDER BY occurred_at ASC",
                    (request_id,),
                ).fetchall()
            else:
                rows = self._reconcile_conn.execute(
                    "SELECT * FROM local_audit_event ORDER BY occurred_at ASC LIMIT 100"
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
        if status not in {"in-sync", "conflict"}:
            raise ValueError(f"invalid reconcile status: {status}")
        with self._lock:
            self._reconcile_conn.execute(
                """UPDATE reconcile_state SET
                     reconcile_status = ?, reconciled_at = ?
                   WHERE module_id = ? AND resource_id = ?""",
                (status, timestamp, module_id, resource_id),
            )
            self._reconcile_conn.commit()

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

    def _failover_permitted(self) -> bool:
        try:
            return bool(self._policy_allows_failover())
        except Exception:
            return False

    def _check_bounded_limits(self) -> None:
        """Fail-closed if any SQLite bounded hard limit is exceeded."""
        limits = self._bounded_limits
        # 1. Max pending count
        pending = self.pending_count()
        if pending >= limits.max_pending_count:
            self._stats.record_fail_closed()
            raise FailoverBoundedLimitError(
                f"max_pending_count exceeded: {pending} >= {limits.max_pending_count}"
            )
        # 2. Max DB size
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
        # 3. Max degraded time
        if self._degraded_since is not None:
            degraded_elapsed = time.monotonic() - self._degraded_since
            if degraded_elapsed >= limits.max_degraded_seconds:
                self._stats.record_fail_closed()
                raise FailoverBoundedLimitError(
                    f"max_degraded_seconds exceeded: {degraded_elapsed:.1f} >= {limits.max_degraded_seconds}"
                )
        # 4. Reconcile deadline
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
            if self._failover_permitted():
                return self._call_fallback(operation, fn_fallback, *args)
            raise
        except Exception as exc:
            self._stats.record_pg_failure()
            _logger.warning("failover_store: primary %s failed: %s", operation, exc)
            if self._failover_permitted():
                return self._call_fallback(operation, fn_fallback, *args)
            raise

    def _call_fallback(self, operation: str, fn: Callable[..., Any], *args: Any) -> Any:
        # Fail-closed: check bounded limits before writing to SQLite.
        self._check_bounded_limits()
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

    def reconcile(
        self,
        pg_connection: Any,
        *,
        batch_size: int = 100,
    ) -> dict[str, Any]:
        """Run one-directional SQLite → PostgreSQL reconciliation.

        Returns a summary dict with counts per action.  Conflicts are
        flagged, not auto-resolved.  Pending rows are cleared only after
        successful reconciliation.
        """
        from core_system.data_reconciliation import ReconcileService

        with self._lock:
            self._stats.reconciliations_run += 1
            if self._reconcile_started_at is None:
                self._reconcile_started_at = time.monotonic()
            service = ReconcileService(self._reconcile_conn, pg_connection)
            summary: dict[str, int] = {
                "pushed": 0,
                "pulled": 0,
                "in-sync": 0,
                "conflict": 0,
                "skipped": 0,
            }
            # Group pending by module_id
            modules = self._reconcile_conn.execute(
                "SELECT DISTINCT module_id FROM reconcile_state WHERE reconcile_status = 'pending'"
            ).fetchall()
            for mod_row in modules:
                module_id = str(mod_row["module_id"])
                for result in service.reconcile_module(module_id, batch_size=batch_size):
                    summary[result.action] = summary.get(result.action, 0) + 1
                    if result.action == "pushed":
                        self._stats.reconciliation_pushed += 1
                    elif result.action == "pulled":
                        self._stats.reconciliation_pulled += 1
                    elif result.action == "conflict":
                        self._stats.reconciliation_conflicts += 1
                    elif result.action == "skipped":
                        self._stats.reconciliation_skipped += 1
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
            "bounded_limits": {
                "max_pending_count": self._bounded_limits.max_pending_count,
                "max_db_size_bytes": self._bounded_limits.max_db_size_bytes,
                "max_wal_size_bytes": self._bounded_limits.max_wal_size_bytes,
                "max_degraded_seconds": self._bounded_limits.max_degraded_seconds,
                "reconcile_deadline_seconds": self._bounded_limits.reconcile_deadline_seconds,
            },
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
]
