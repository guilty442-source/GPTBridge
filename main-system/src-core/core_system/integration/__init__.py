"""Composition root for the main-system data-platform and RAG assemblies.

Workflow E wiring only: these factories instantiate already-governed
components from injected dependencies and never invent business logic.

  * :func:`build_saga_services` — Saga runtime over the migration-113
    operation authority (``PostgresSagaStore`` + handlers + reconciler).
  * :func:`build_failover_store` — bounded SQLite failover store with the
    A508/A509/A512 hooks (contract, generation, identity, permission,
    SQLite scope).
  * :func:`build_reconcile_callback` — routes parked operations to the
    existing ``ReconcileService`` decision.
  * :func:`build_rag_router` — canonical/degraded backends plus the
    ``RagArchitectureRouter`` (``search`` stays async).

All factories are fail-closed: missing dependencies raise
:class:`IntegrationWireError` before any object is constructed.
"""

from .data_platform import (
    DEFAULT_RECONCILE_BATCH,
    DEFAULT_RECONCILE_WORKER,
    DEFAULT_SAGA_WORKER,
    IntegrationWireError,
    SagaServices,
    build_failover_store,
    build_reconcile_callback,
    build_saga_services,
)
from .rag_platform import RagServices, build_rag_router

__all__ = [
    "DEFAULT_RECONCILE_BATCH",
    "DEFAULT_RECONCILE_WORKER",
    "DEFAULT_SAGA_WORKER",
    "IntegrationWireError",
    "RagServices",
    "SagaServices",
    "build_failover_store",
    "build_rag_router",
    "build_reconcile_callback",
    "build_saga_services",
]
