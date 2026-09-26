"""Saga runtime integration — module binding reconcile through the operation authority.

Assembles the migration-113 Saga runtime controlled by main-system and routes
module reconciliation through it:

* ``SagaRuntimeIntegration.start`` builds ``SagaServices`` (durable
  ``PostgresSagaStore`` + step handlers + reconcile callback) and exposes it as
  ``app.saga_runtime``;
* the single production operation is **module binding reconcile**: with the
  module-private SQLite authority retired (A610/A621), PostgreSQL is the sole
  structured-data authority, so the reconcile step completes as a truthful
  no-op — there is nothing left to reconcile;
* maintenance-controller reconcile executors are re-registered to execute
  through the saga operation when the runtime is available (the original
  executor stays as a fail-open fallback);
* the reconcile callback resolves the operation only when the pending count
  reaches zero — conflicts and unavailable PostgreSQL keep it parked.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from shared_layer.database.config import DatabaseSettings
from shared_layer.database.workload_lanes import WorkloadClass, get_lane_pool
from shared_layer.workflow import (
    Engine,
    Operation,
    OutcomeStrategy,
    ReconcileDecision,
    ReconcileVerdict,
    StepPlan,
    StepResult,
    StepSpec,
    StepStatus,
)
from shared_layer.workflow.handlers import CallableStepHandler, StepHandlerRegistry

from .integration.data_platform import SagaServices, build_saga_services

SAGA_WORKER = "main-system-saga"
MODULE_RECONCILE_OPERATION = "module-binding-reconcile"
MODULE_RECONCILE_ACTION = "reconcile-module"
MODULE_RECONCILE_STEP = "module-reconcile-step"
DEFAULT_BATCH_SIZE = 100


class SagaRuntimeIntegration:
    """Assembled Saga authority plus the module-reconcile operation."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.services: SagaServices | None = None
        self.started_at = 0.0

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        self.services = build_saga_services(
            lambda: get_lane_pool().connection(WorkloadClass.BACKGROUND),
            self._handler_registry(),
            self._reconcile_callback,
            worker=SAGA_WORKER,
        )
        self.app.saga_runtime = self
        self.started_at = time.time()
        self._wrap_maintenance_reconcile_executors()
        return {"ok": True, "worker": SAGA_WORKER}

    def stop(self) -> None:
        self.services = None
        self.started_at = 0.0

    # -- operation -------------------------------------------------------

    def run_module_reconcile(
        self,
        module_id: str,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        idempotency_key: str = "",
    ) -> Any:
        """Run one module binding reconcile through the saga authority."""
        if self.services is None:
            raise RuntimeError("SAGA_RUNTIME_NOT_STARTED")
        operation = Operation(
            operation_id=f"saga-reconcile-{module_id}-{uuid.uuid4().hex[:12]}",
            operation_type=MODULE_RECONCILE_OPERATION,
            module_id=module_id,
            idempotency_key=idempotency_key
            or f"module-reconcile:{module_id}:{int(time.time())}",
            correlation_id=f"maintenance-reconcile:{module_id}",
            checkpoint={"batch_size": int(batch_size)},
        )
        plan = StepPlan(
            operation_type=MODULE_RECONCILE_OPERATION,
            steps=(
                StepSpec(
                    step_id=MODULE_RECONCILE_STEP,
                    step_order=1,
                    engine=Engine.POSTGRESQL,
                    action_type=MODULE_RECONCILE_ACTION,
                    strategy=OutcomeStrategy.RETRY_IDEMPOTENT,
                    idempotent=True,
                    description="PostgreSQL module binding reconcile (SQLite authority retired)",
                ),
            ),
        )
        return self.services.run(operation, plan)

    # -- handlers --------------------------------------------------------

    def _handler_registry(self) -> StepHandlerRegistry:
        registry = StepHandlerRegistry()
        registry.register_action(
            MODULE_RECONCILE_ACTION,
            CallableStepHandler(
                perform_callable=self._perform_reconcile,
                verify_callable=self._verify_reconcile,
            ),
        )
        return registry

    def _perform_reconcile(self, operation: Operation, step: StepSpec) -> StepResult:
        module_id = str(operation.module_id)
        # A610/A621: no module-private SQLite owner DB remains —
        # PostgreSQL is the sole structured-data authority, so there
        # is literally nothing left to reconcile for this module.
        operation.checkpoint["pending_after"] = 0
        operation.checkpoint["actions"] = {"retired-sqlite-authority": 1}
        return StepResult(
            step.step_id,
            StepStatus.COMPLETED.value,
            detail=f"pending_after=0 retired-sqlite-authority:{module_id}",
            payload={"actions": {"retired-sqlite-authority": 1}, "pending_after": 0},
        )

    def _verify_reconcile(self, operation: Operation, step: StepSpec) -> bool:
        # No SQLite owner DB → PostgreSQL is already authoritative.
        return True

    def _reconcile_callback(self, operation: Operation) -> ReconcileVerdict:
        pending_after = operation.checkpoint.get("pending_after")
        if pending_after is None:
            return ReconcileVerdict(
                ReconcileDecision.PENDING, detail="RECONCILE_NOT_EXECUTED"
            )
        if int(pending_after) == 0:
            return ReconcileVerdict(ReconcileDecision.RESOLVED, detail="pending-zero")
        return ReconcileVerdict(
            ReconcileDecision.PENDING, detail=f"pending-{int(pending_after)}"
        )

    # -- maintenance controller wiring -----------------------------------

    def _wrap_maintenance_reconcile_executors(self) -> None:
        integration = getattr(self.app, "maintenance_controller_integration", None)
        controller = getattr(integration, "controller", None)
        if controller is None:
            return
        from shared_layer.database.maintenance.maintenance_reconcile import (
            get_reconcile_maintenance_executors,
        )

        for action_id, fallback in get_reconcile_maintenance_executors().items():
            controller.set_executor(
                action_id, self._saga_executor(action_id, fallback)
            )

    def _saga_executor(self, action_id: str, fallback: Any) -> Any:
        def execute(job: Any, action: Any, before_state: dict[str, Any]) -> dict[str, Any]:
            module_id = str(getattr(job, "module_id", "") or "")
            if not module_id or self.services is None:
                return fallback(job, action, before_state)
            try:
                receipt = self.run_module_reconcile(module_id)
            except Exception:
                return fallback(job, action, before_state)
            operation = receipt.operation
            after_state = dict(before_state)
            after_state["saga_operation_id"] = operation.operation_id
            after_state["saga_status"] = operation.status.value
            after_state["saga_actions"] = operation.checkpoint.get("actions", {})
            after_state["saga_pending_after"] = operation.checkpoint.get("pending_after")
            after_state["maintenance_verified"] = (
                operation.status.value == "COMPLETED"
            )
            return after_state

        execute.__name__ = f"saga_{action_id}"
        return execute


def create_saga_runtime_integration(app: Any) -> SagaRuntimeIntegration:
    return SagaRuntimeIntegration(app)


__all__ = [
    "SagaRuntimeIntegration",
    "create_saga_runtime_integration",
]
