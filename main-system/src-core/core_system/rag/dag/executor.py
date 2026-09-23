"""RAG DAG executor — bounded, fail-closed DAG execution (A549).

The executor runs a validated plan in deterministic topological order with
per-node timeout, bounded retries, cancellation and compensation.  Unknown
node handlers quarantine the run, missing required evidence fails the run,
and a timeout cancels the run; no outcome is ever fabricated.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time
from dataclasses import replace
from typing import Any, Callable, Mapping

from .contracts import (
    REGISTERED_NODE_CATALOG,
    TERMINAL_STATES,
    RagDagExecutionContext,
    RagDagExecutionResult,
    RagDagNode,
    RagDagNodeResult,
    RagDagNodeType,
    RagDagPlan,
    RagDagState,
)
from ..observability import RAG_METRICS

NodeHandler = Callable[[RagDagNode, RagDagExecutionContext, Mapping[str, Any]], Any]
CompensationHandler = Callable[[RagDagNode, Mapping[str, Any]], Any]

DEFAULT_NODE_TIMEOUT_SECONDS = 30.0


class RagDagExecutionError(RuntimeError):
    """Raised only for programmer errors; execution outcomes are returned."""

    failure_code = "RAG_DAG_EXECUTION_ERROR"


class RagDagExecutor:
    """Executes a validated DAG plan with bounded, fail-closed semantics."""

    def __init__(
        self,
        handlers: Mapping[RagDagNodeType, NodeHandler],
        *,
        compensations: Mapping[RagDagNodeType, CompensationHandler] | None = None,
        node_timeout_seconds: float = DEFAULT_NODE_TIMEOUT_SECONDS,
        retry_limit: int = 0,
    ) -> None:
        self._handlers = dict(handlers)
        self._compensations = dict(compensations or {})
        self._node_timeout = max(0.1, float(node_timeout_seconds))
        self._retry_limit = max(0, int(retry_limit))

    def execute(
        self,
        plan: RagDagPlan,
        *,
        cancel_event: Any | None = None,
    ) -> RagDagExecutionResult:
        started = time.monotonic()
        upstream: dict[str, Mapping[str, Any]] = {}
        results: list[RagDagNodeResult] = []
        failure_reasons: list[str] = []
        compensations: dict[str, Any] = {}
        execution_state = RagDagState.SUCCEEDED
        succeeded_side_effects: list[RagDagNode] = []

        order = self._topological_order(plan)
        for node in order:
            if self._cancelled(plan.context, cancel_event):
                execution_state = RagDagState.CANCELLED
                failure_reasons.append("execution-cancelled")
                break

            spec = REGISTERED_NODE_CATALOG.get(node.node_type)
            if spec is None:
                label = getattr(node.node_type, "value", node.node_type)
                results.append(
                    RagDagNodeResult(
                        node_id=node.node_id,
                        node_type=node.node_type,
                        state=RagDagState.QUARANTINED,
                        attempts=0,
                        latency_ms=0,
                        error=f"unregistered-node-type:{label}",
                    )
                )
                execution_state = RagDagState.QUARANTINED
                failure_reasons.append(f"unregistered-node-type:{label}")
                break

            handler = self._handlers.get(node.node_type)
            if handler is None:
                results.append(
                    RagDagNodeResult(
                        node_id=node.node_id,
                        node_type=node.node_type,
                        state=RagDagState.QUARANTINED,
                        attempts=0,
                        latency_ms=0,
                        error=f"unregistered-handler:{node.node_type.value}",
                    )
                )
                execution_state = RagDagState.QUARANTINED
                failure_reasons.append(f"unregistered-handler:{node.node_type.value}")
                break

            node_started = time.monotonic()
            outcome, error, attempts = self._run_with_retries(
                handler, node, plan.context, upstream
            )
            latency_ms = int((time.monotonic() - node_started) * 1000)
            # §10.11: feed per-stage latency into the RAG metrics surface
            # consumed by the perf-baseline snapshot.
            RAG_METRICS.observe_stage(node.node_type.value, latency_ms)

            if error == "node-timeout":
                results.append(
                    RagDagNodeResult(
                        node_id=node.node_id,
                        node_type=node.node_type,
                        state=RagDagState.CANCELLED,
                        attempts=attempts,
                        latency_ms=latency_ms,
                        error=error,
                    )
                )
                execution_state = RagDagState.CANCELLED
                failure_reasons.append(f"node-timeout:{node.node_id}")
                break

            evidence = dict(outcome.get("evidence") or {})
            missing = [
                key for key in spec.required_evidence if key not in evidence
            ]
            if error or outcome.get("ok") is not True or missing:
                reason = error or (
                    f"evidence-incomplete:{','.join(missing)}" if missing else "handler-not-ok"
                )
                results.append(
                    RagDagNodeResult(
                        node_id=node.node_id,
                        node_type=node.node_type,
                        state=RagDagState.FAILED,
                        attempts=attempts,
                        latency_ms=latency_ms,
                        evidence=evidence,
                        error=reason,
                    )
                )
                execution_state = RagDagState.FAILED
                failure_reasons.append(f"{reason}:{node.node_id}")
                break

            upstream[node.node_id] = evidence
            results.append(
                RagDagNodeResult(
                    node_id=node.node_id,
                    node_type=node.node_type,
                    state=RagDagState.SUCCEEDED,
                    attempts=attempts,
                    latency_ms=latency_ms,
                    evidence=evidence,
                )
            )
            if spec.side_effecting:
                succeeded_side_effects.append(node)

        if execution_state in {RagDagState.FAILED, RagDagState.QUARANTINED, RagDagState.CANCELLED}:
            compensation_state, compensations = self._compensate(
                succeeded_side_effects, upstream
            )
            if compensation_state == RagDagState.REQUIRES_RECONCILE:
                execution_state = RagDagState.REQUIRES_RECONCILE
                failure_reasons.append("compensation-requires-reconcile")

        latency_ms = int((time.monotonic() - started) * 1000)
        digest = _digest(
            {
                "dag_id": plan.dag_id,
                "kind": plan.kind.value,
                "state": execution_state.value,
                "nodes": [result.to_record() for result in results],
                "compensations": compensations,
            }
        )
        return RagDagExecutionResult(
            dag_id=plan.dag_id,
            kind=plan.kind,
            state=execution_state,
            execution_id=plan.context.execution_id,
            correlation_id=plan.context.correlation_id,
            node_results=tuple(results),
            latency_ms=latency_ms,
            evidence_digest=digest,
            failure_reasons=tuple(failure_reasons),
            compensations=compensations,
        )

    def _run_with_retries(
        self,
        handler: NodeHandler,
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], str, int]:
        attempts = 0
        last_error = ""
        while attempts <= self._retry_limit:
            attempts += 1
            try:
                outcome = self._run_with_timeout(handler, node, context, upstream)
            except TimeoutError:
                return {}, "node-timeout", attempts
            except Exception as exc:  # noqa: BLE001 - record the failure type
                last_error = f"handler-exception:{type(exc).__name__}"
                continue
            if not isinstance(outcome, Mapping):
                last_error = "handler-non-mapping"
                continue
            return outcome, "", attempts
        return {}, last_error or "handler-not-ok", attempts

    def _run_with_timeout(
        self,
        handler: NodeHandler,
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(handler, node, context, dict(upstream))
            try:
                return future.result(timeout=self._node_timeout)
            except concurrent.futures.TimeoutError as error:
                future.cancel()
                raise TimeoutError("node-timeout") from error

    def _compensate(
        self,
        nodes: list[RagDagNode],
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> tuple[RagDagState, dict[str, Any]]:
        outcomes: dict[str, Any] = {}
        state = RagDagState.SUCCEEDED
        for node in reversed(nodes):
            handler = self._compensations.get(node.node_type)
            if handler is None:
                outcomes[node.node_id] = "no-compensation-registered"
                state = RagDagState.REQUIRES_RECONCILE
                continue
            try:
                handler(node, dict(upstream.get(node.node_id) or {}))
            except Exception as exc:  # noqa: BLE001 - compensation failure is recorded
                outcomes[node.node_id] = f"compensation-failed:{type(exc).__name__}"
                state = RagDagState.REQUIRES_RECONCILE
            else:
                outcomes[node.node_id] = "compensated"
        return state, outcomes

    @staticmethod
    def _cancelled(context: RagDagExecutionContext, cancel_event: Any | None) -> bool:
        if context.cancel_requested:
            return True
        if cancel_event is None:
            return False
        try:
            return bool(cancel_event.is_set())
        except AttributeError:
            return bool(cancel_event)

    @staticmethod
    def _topological_order(plan: RagDagPlan) -> list[RagDagNode]:
        indegree = {node.node_id: 0 for node in plan.nodes}
        adjacency: dict[str, list[str]] = {node.node_id: [] for node in plan.nodes}
        for edge in plan.edges:
            adjacency[edge.source_id].append(edge.target_id)
            indegree[edge.target_id] += 1
        ready = [node.node_id for node in plan.nodes if indegree[node.node_id] == 0]
        order: list[RagDagNode] = []
        while ready:
            node_id = ready.pop(0)
            order.append(plan.node(node_id))
            for target in adjacency[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if len(order) != len(plan.nodes):
            raise RagDagExecutionError("dag-cycle")
        return order


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


__all__ = [
    "DEFAULT_NODE_TIMEOUT_SECONDS",
    "RagDagExecutionError",
    "RagDagExecutor",
]
