"""Composition root for the main-system data-platform assemblies.

The factories here only *wire* already-governed components:

  * cross-engine Saga runtime (:mod:`shared_layer.workflow`),
  * bounded SQLite failover store (:mod:`shared_layer.failover_store`)
    with its A508/A509/A512 hooks,
  * reconciliation authority
    (:class:`core_system.data_reconciliation.ReconcileService`).

No business rule is invented here: the reconcile callback translates the
``ReconcileService`` verdicts into the worker's declared state machine and
never decides a conflict winner itself.

Every factory is injection-based and fail-closed: a missing or invalid
dependency raises :class:`IntegrationWireError` *before* any object is
constructed, so a half-assembled store can never be returned to a caller.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from shared_layer.failover_store import (
    BoundedLimits,
    FailoverSharedLayerStore,
    ReconciliationContractError,
    SqliteReconciliationContract,
    load_reconciliation_contract,
)
from shared_layer.security.generation import GenerationLedger
from shared_layer.security.sqlite_scope import (
    SqliteScopeBinding,
    SqliteScopeError,
    assert_binding,
)
from shared_layer.workflow import (
    Operation,
    PostgresSagaStore,
    ReconcileCallback,
    ReconcileDecision,
    ReconcileReceipt,
    ReconcileVerdict,
    ReconcileVerdictError,
    ReconcileWorker,
    RunReceipt,
    SagaStore,
    StepPlan,
    run_operation,
)

from ..data_reconciliation import ReconcileResult, ReconcileService

DEFAULT_SAGA_WORKER = "main-system-saga"
DEFAULT_RECONCILE_WORKER = "main-system-reconcile"
DEFAULT_RECONCILE_BATCH = 100

# ReconcileService action -> ReconcileWorker verdict.  This is a translation
# of the service's own decision, never a new arbitration rule: a conflict is
# parked for an authorized owner, an unavailable PostgreSQL stays pending.
_ACTION_VERDICTS: dict[str, ReconcileDecision] = {
    "pushed": ReconcileDecision.RESOLVED,
    "pulled": ReconcileDecision.RESOLVED,
    "in-sync": ReconcileDecision.RESOLVED,
    "conflict": ReconcileDecision.QUARANTINED,
    "skipped": ReconcileDecision.PENDING,
}


class IntegrationWireError(RuntimeError):
    """Raised when a composition-root dependency is missing (fail-closed)."""


@dataclass
class SagaServices:
    """Assembled Saga runtime: durable store, handlers and reconciler."""

    store: SagaStore
    handlers: Any
    reconcile_callback: ReconcileCallback
    worker: str = DEFAULT_SAGA_WORKER
    lease_seconds: float = 120.0

    def run(
        self,
        operation: Operation,
        plan: StepPlan,
        *,
        worker: Optional[str] = None,
        **runtime_kwargs: Any,
    ) -> RunReceipt:
        """Run one operation through the injected handlers."""
        return run_operation(
            operation,
            plan,
            self.handlers,
            store=self.store,
            worker=worker or self.worker,
            **runtime_kwargs,
        )

    def reconcile_worker(self, *, worker: Optional[str] = None) -> ReconcileWorker:
        return ReconcileWorker(
            self.store,
            worker=worker or DEFAULT_RECONCILE_WORKER,
            lease_seconds=self.lease_seconds,
        )

    def reconcile_once(self, *, worker: Optional[str] = None) -> ReconcileReceipt:
        return self.reconcile_worker(worker=worker).run_once(self.reconcile_callback)

    def drain_reconcile(
        self, *, worker: Optional[str] = None, limit: int = 100
    ) -> list[ReconcileReceipt]:
        return self.reconcile_worker(worker=worker).drain(
            self.reconcile_callback, limit=limit
        )


def build_saga_services(
    connection_provider: Callable[[], Any],
    handlers: Any,
    reconcile_callback: ReconcileCallback,
    *,
    worker: str = DEFAULT_SAGA_WORKER,
    lease_seconds: float = 120.0,
) -> SagaServices:
    """Assemble the Saga runtime for the migration-113 operation authority.

    ``connection_provider`` must be a callable returning a connection
    context manager (for example ``pool.acquire``).  Without it, or without
    handlers / a reconcile callback, nothing is constructed.
    """
    if not callable(connection_provider):
        raise IntegrationWireError("SAGA_CONNECTION_PROVIDER_REQUIRED")
    _assert_handlers(handlers)
    if not callable(reconcile_callback):
        raise IntegrationWireError("SAGA_RECONCILE_CALLBACK_REQUIRED")
    store = PostgresSagaStore(connection_provider)
    return SagaServices(
        store=store,
        handlers=handlers,
        reconcile_callback=reconcile_callback,
        worker=str(worker or DEFAULT_SAGA_WORKER),
        lease_seconds=float(lease_seconds),
    )


def build_reconcile_callback(
    service_factory: Callable[[], ReconcileService],
    *,
    batch_size: int = DEFAULT_RECONCILE_BATCH,
) -> ReconcileCallback:
    """Bind the saga reconcile worker to ``ReconcileService``.

    ``service_factory`` is invoked per reconcile cycle so the caller owns
    connection lifetime.  The callback selects the service result for the
    operation's resource and returns the translated verdict; an unknown
    action raises (the worker then keeps the operation parked).
    """
    if not callable(service_factory):
        raise IntegrationWireError("RECONCILE_SERVICE_FACTORY_REQUIRED")
    size = max(1, int(batch_size))

    def reconcile(operation: Operation) -> ReconcileVerdict:
        module_id = str(getattr(operation, "module_id", "") or "").strip()
        if not module_id:
            raise ReconcileVerdictError("RECONCILE_MODULE_REQUIRED")
        service = service_factory()
        if service is None:
            raise ReconcileVerdictError("RECONCILE_SERVICE_UNAVAILABLE")
        reconcile_module = getattr(service, "reconcile_module", None)
        if not callable(reconcile_module):
            raise ReconcileVerdictError("RECONCILE_SERVICE_MISSING_RECONCILE_MODULE")
        resource_id = str(getattr(operation, "resource_id", "") or "")
        selected: Optional[ReconcileResult] = None
        for result in reconcile_module(module_id, batch_size=size):
            if not resource_id or str(result.resource_id) == resource_id:
                selected = result
                break
        return _verdict_for(selected)

    return reconcile


def build_failover_store(
    *,
    primary_store: Any,
    fallback_store: Any,
    reconcile_connection: sqlite3.Connection,
    policy_allows_failover: Callable[[], bool],
    generation_provider: Callable[[], Optional[int]],
    pg_identity_resolver: Callable[[Any], Optional[str]],
    permission_revalidator: Callable[[str, str, dict[str, Any]], bool],
    contract: Optional[SqliteReconciliationContract] = None,
    codex_db_path: Optional[str] = None,
    db_path: Optional[str] = None,
    sqlite_scope: Optional[SqliteScopeBinding] = None,
    bounded_limits: Optional[BoundedLimits] = None,
    generation_ledger: Optional[GenerationLedger] = None,
    pg_commit_identity_provider: Optional[Callable[..., Optional[str]]] = None,
    config: Any = None,
) -> FailoverSharedLayerStore:
    """Assemble the A508/A509/A512 failover store from injected hooks.

    A file-backed SQLite buffer always requires an A512
    :class:`SqliteScopeBinding` that verifies for the declared path; a
    missing contract, hook or scope fails closed before construction.
    """
    if primary_store is None:
        raise IntegrationWireError("FAILOVER_PRIMARY_STORE_REQUIRED")
    if fallback_store is None:
        raise IntegrationWireError("FAILOVER_FALLBACK_STORE_REQUIRED")
    if not isinstance(reconcile_connection, sqlite3.Connection):
        raise IntegrationWireError("FAILOVER_RECONCILE_CONNECTION_REQUIRED")
    for name, hook in (
        ("policy_allows_failover", policy_allows_failover),
        ("generation_provider", generation_provider),
        ("pg_identity_resolver", pg_identity_resolver),
        ("permission_revalidator", permission_revalidator),
    ):
        if not callable(hook):
            raise IntegrationWireError(f"FAILOVER_HOOK_REQUIRED:{name}")

    if contract is None:
        if not (codex_db_path and str(codex_db_path).strip()):
            raise IntegrationWireError("FAILOVER_A509_CONTRACT_REQUIRED")
        try:
            contract = load_reconciliation_contract(codex_db_path)
        except ReconciliationContractError as error:
            raise IntegrationWireError(
                f"FAILOVER_A509_CONTRACT_UNLOADABLE:{error}"
            ) from error

    if sqlite_scope is None:
        raise IntegrationWireError("FAILOVER_A512_SQLITE_SCOPE_REQUIRED")
    declared_path = str(db_path or sqlite_scope.path or "").strip()
    if not declared_path or declared_path == ":memory:":
        raise IntegrationWireError("FAILOVER_SQLITE_PATH_REQUIRED")
    try:
        assert_binding(sqlite_scope, declared_path)
    except SqliteScopeError as error:
        raise IntegrationWireError(
            f"FAILOVER_A512_SQLITE_SCOPE_UNVERIFIED:{error}"
        ) from error

    return FailoverSharedLayerStore(
        primary_store,
        fallback_store,
        reconcile_connection,
        policy_allows_failover=policy_allows_failover,
        config=config,
        bounded_limits=bounded_limits,
        db_path=declared_path,
        contract=contract,
        generation_provider=generation_provider,
        generation_ledger=generation_ledger,
        pg_identity_resolver=pg_identity_resolver,
        pg_commit_identity_provider=pg_commit_identity_provider,
        permission_revalidator=permission_revalidator,
        sqlite_scope=sqlite_scope,
        reconcile_service_factory=(
            lambda local, pg: ReconcileService(local, pg)
        ),
    )


def _assert_handlers(handlers: Any) -> None:
    if handlers is None:
        raise IntegrationWireError("SAGA_HANDLERS_REQUIRED")
    if isinstance(handlers, Mapping):
        if not handlers:
            raise IntegrationWireError("SAGA_HANDLERS_REQUIRED")
        return
    if callable(getattr(handlers, "resolve", None)) or callable(
        getattr(handlers, "missing", None)
    ):
        return
    raise IntegrationWireError("SAGA_HANDLERS_REQUIRED")


def _verdict_for(result: Optional[ReconcileResult]) -> ReconcileVerdict:
    if result is None:
        return ReconcileVerdict(
            ReconcileDecision.PENDING, detail="RECONCILE_ROW_ABSENT"
        )
    action = str(result.action)
    decision = _ACTION_VERDICTS.get(action)
    if decision is None:
        raise ReconcileVerdictError(f"RECONCILE_ACTION_UNKNOWN:{action}")
    detail = ":".join(part for part in (action, str(result.detail or "")) if part)
    return ReconcileVerdict(decision, detail=detail)


__all__ = [
    "DEFAULT_RECONCILE_BATCH",
    "DEFAULT_RECONCILE_WORKER",
    "DEFAULT_SAGA_WORKER",
    "IntegrationWireError",
    "SagaServices",
    "build_failover_store",
    "build_reconcile_callback",
    "build_saga_services",
]
