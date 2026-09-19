"""Saga Visualization Module.

Provides data processing and rendering for Saga operation visualization.
Generates DAG graphs, timelines, and state transition diagrams for
cross-engine workflow operations.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .types import (
    Engine,
    OperationStatus,
    OutcomeStrategy,
    StepStatus,
    SAGA_EVENTS,
    TERMINAL_STATUSES,
)
from .operation import Operation
from .steps import StepPlan, StepResult, StepSpec


class VisualizationFormat(Enum):
    """Supported output formats for Saga visualization."""
    MERMAID = "mermaid"
    GRAPHVIZ_DOT = "dot"
    JSON = "json"
    ASCII = "ascii"
    TIMELINE = "timeline"


@dataclass
class SagaNode:
    """A node in the Saga DAG (step or operation)."""
    node_id: str
    label: str
    node_type: str  # "operation", "step", "compensation"
    status: str
    engine: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    position: dict[str, float] = field(default_factory=dict)


@dataclass
class SagaEdge:
    """An edge in the Saga DAG (dependency or transition)."""
    from_node: str
    to_node: str
    edge_type: str  # "depends_on", "transition", "compensation", "heartbeat"
    label: str = ""


@dataclass
class SagaTimelineEvent:
    """A timestamped event in the Saga timeline."""
    timestamp: float
    event_type: str
    operation_id: str
    step_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    status: str = ""


@dataclass
class SagaVisualizationData:
    """Complete visualization data for a Saga operation."""
    operation_id: str
    operation_type: str
    module_id: str
    status: str
    nodes: list[SagaNode] = field(default_factory=list)
    edges: list[SagaEdge] = field(default_factory=list)
    timeline: list[SagaTimelineEvent] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class SagaVisualizer:
    """Generates visualizations from Saga operation data."""

    # Color mapping for statuses
    STATUS_COLORS = {
        OperationStatus.PENDING.value: "#94a3b8",      # slate-400
        OperationStatus.RUNNING.value: "#3b82f6",      # blue-500
        OperationStatus.COMPENSATING.value: "#f59e0b", # amber-500
        OperationStatus.COMPLETED.value: "#22c55e",    # green-500
        OperationStatus.FAILED.value: "#ef4444",       # red-500
        OperationStatus.REQUIRES_RECONCILE.value: "#a855f7", # purple-500
        OperationStatus.QUARANTINED.value: "#6b7280",  # gray-500
    }

    STEP_STATUS_COLORS = {
        StepStatus.PENDING.value: "#94a3b8",
        StepStatus.RUNNING.value: "#3b82f6",
        StepStatus.COMPLETED.value: "#22c55e",
        StepStatus.FAILED.value: "#ef4444",
        StepStatus.TIMEOUT.value: "#f59e0b",
        StepStatus.UNKNOWN.value: "#6b7280",
        StepStatus.COMPENSATED.value: "#a855f7",
        StepStatus.INVALIDATED.value: "#ef4444",
        StepStatus.SUPERSEDED.value: "#a855f7",
        StepStatus.SKIPPED.value: "#94a3b8",
    }

    ENGINE_COLORS = {
        Engine.POSTGRESQL.value: "#336791",    # PostgreSQL blue
        Engine.SQLITE.value: "#003b57",        # SQLite dark blue
        Engine.QDRANT.value: "#f59e0b",        # Qdrant amber
        Engine.FILESYSTEM.value: "#6b7280",    # Filesystem gray
        Engine.MODEL.value: "#a855f7",         # Model purple
    }

    def __init__(self) -> None:
        pass

    def visualize_operation(self, operation: Operation, plan: StepPlan | None = None) -> SagaVisualizationData:
        """Generate visualization data from an Operation and optional StepPlan."""
        nodes = []
        edges = []
        timeline = []

        # Operation node
        op_node = SagaNode(
            node_id=f"op:{operation.operation_id}",
            label=f"{operation.operation_type}\n({operation.module_id})",
            node_type="operation",
            status=operation.status.value,
            engine="",
            details={
                "operation_id": operation.operation_id,
                "module_id": operation.module_id,
                "resource_id": operation.resource_id,
                "generation": operation.generation,
                "idempotency_key": operation.idempotency_key,
                "correlation_id": operation.correlation_id,
            },
            position={"x": 0, "y": 0},
        )
        nodes.append(op_node)

        # Step nodes
        if plan:
            for i, spec in enumerate(plan.ordered()):
                result = operation.steps.get(spec.step_id)
                status = result.status if result else StepStatus.PENDING.value

                step_node = SagaNode(
                    node_id=f"step:{spec.step_id}",
                    label=f"{spec.step_id}\n({spec.action_type})",
                    node_type="step",
                    status=status,
                    engine=spec.engine.value,
                    details={
                        "step_id": spec.step_id,
                        "step_order": spec.step_order,
                        "action_type": spec.action_type,
                        "engine": spec.engine.value,
                        "strategy": spec.strategy.value,
                        "long_running": spec.long_running,
                    },
                    position={"x": 100 + i * 200, "y": 100},
                )
                nodes.append(step_node)

                # Edge from operation to step
                edges.append(SagaEdge(
                    from_node=f"op:{operation.operation_id}",
                    to_node=f"step:{spec.step_id}",
                    edge_type="depends_on",
                    label=f"order {spec.step_order}",
                ))

                # Edges between sequential steps
                if i > 0:
                    prev_spec = plan.ordered()[i - 1]
                    edges.append(SagaEdge(
                        from_node=f"step:{prev_spec.step_id}",
                        to_node=f"step:{spec.step_id}",
                        edge_type="depends_on",
                        label="",
                    ))

                # Compensation edges (dashed)
                if spec.strategy in (OutcomeStrategy.COMPENSATE, OutcomeStrategy.ROLLBACK):
                    edges.append(SagaEdge(
                        from_node=f"step:{spec.step_id}",
                        to_node=f"comp:{spec.step_id}",
                        edge_type="compensation",
                        label=spec.strategy.value,
                    ))

        # Add compensation nodes
        for spec in plan.ordered() if plan else []:
            if spec.strategy in (OutcomeStrategy.COMPENSATE, OutcomeStrategy.ROLLBACK, OutcomeStrategy.RECONCILE):
                comp_node = SagaNode(
                    node_id=f"comp:{spec.step_id}",
                    label=f"↩ {spec.step_id}\n({spec.strategy.value})",
                    node_type="compensation",
                    status=StepStatus.PENDING.value,
                    engine=spec.engine.value,
                    details={
                        "step_id": spec.step_id,
                        "strategy": spec.strategy.value,
                    },
                    position={"x": 100 + plan.ordered().index(spec) * 200, "y": -100},
                )
                nodes.append(comp_node)

        # Build timeline from operation history and step results
        timeline = self._build_timeline(operation)

        return SagaVisualizationData(
            operation_id=operation.operation_id,
            operation_type=operation.operation_type,
            module_id=operation.module_id,
            status=operation.status.value,
            nodes=nodes,
            edges=edges,
            timeline=timeline,
            metadata={
                "generation": operation.generation,
                "module_id": operation.module_id,
                "resource_id": operation.resource_id,
            },
        )

    def visualize_stored_operation(
        self,
        operation: Operation,
        step_rows: list[dict[str, Any]] | None = None,
    ) -> SagaVisualizationData:
        """Build visualization data from a stored operation and raw step rows.

        ``operation_step`` rows persist ``step_order`` / ``engine`` /
        ``action_type`` / ``status`` but not the design-time ``StepSpec``
        fields (``strategy``, ``long_running``), so a faithful ``StepPlan``
        cannot be reconstructed — step nodes are projected directly.
        """
        nodes = [
            SagaNode(
                node_id=f"op:{operation.operation_id}",
                label=f"{operation.operation_type}\n({operation.module_id})",
                node_type="operation",
                status=operation.status.value,
                details={
                    "operation_id": operation.operation_id,
                    "module_id": operation.module_id,
                    "resource_id": operation.resource_id,
                    "generation": operation.generation,
                    "idempotency_key": operation.idempotency_key,
                    "correlation_id": operation.correlation_id,
                },
                position={"x": 0, "y": 0},
            )
        ]
        edges: list[SagaEdge] = []

        ordered = sorted(
            step_rows or (),
            key=lambda row: (int(row.get("step_order") or 0), str(row.get("step_id") or "")),
        )
        for i, row in enumerate(ordered):
            step_id = str(row.get("step_id") or "")
            nodes.append(
                SagaNode(
                    node_id=f"step:{step_id}",
                    label=f"{step_id}\n({row.get('action_type') or ''})",
                    node_type="step",
                    status=str(row.get("status") or "PENDING"),
                    engine=str(row.get("engine") or ""),
                    details={
                        "step_id": step_id,
                        "step_order": int(row.get("step_order") or 0),
                        "action_type": str(row.get("action_type") or ""),
                        "engine": str(row.get("engine") or ""),
                        "attempt_count": int(row.get("attempt_count") or 0),
                        "error_code": str(row.get("error_code") or ""),
                    },
                    position={"x": 100 + i * 200, "y": 100},
                )
            )
            edges.append(SagaEdge(
                from_node=f"op:{operation.operation_id}",
                to_node=f"step:{step_id}",
                edge_type="depends_on",
                label=f"order {int(row.get('step_order') or 0)}",
            ))
            if i > 0:
                prev_id = str(ordered[i - 1].get("step_id") or "")
                edges.append(SagaEdge(
                    from_node=f"step:{prev_id}",
                    to_node=f"step:{step_id}",
                    edge_type="depends_on",
                ))

        return SagaVisualizationData(
            operation_id=operation.operation_id,
            operation_type=operation.operation_type,
            module_id=operation.module_id,
            status=operation.status.value,
            nodes=nodes,
            edges=edges,
            timeline=self._build_timeline(operation),
            metadata={
                "generation": operation.generation,
                "module_id": operation.module_id,
                "resource_id": operation.resource_id,
            },
        )

    def _build_timeline(self, operation: Operation) -> list[dict[str, Any]]:
        """Build timeline from operation history and step results."""
        events = []

        # Add operation creation (approximate from created_at)
        if operation.created_at > 0:
            events.append({
                "timestamp": operation.created_at,
                "event_type": "operation_created",
                "operation_id": operation.operation_id,
                "step_id": "",
                "detail": {"operation_type": operation.operation_type},
                "status": "created",
            })

        # Add state transitions from history
        for i, transition in enumerate(operation.history):
            if "->" in transition:
                from_status, to_status = transition.split("->")
                events.append({
                    "timestamp": operation.updated_at - (len(operation.history) - i) * 10,  # approximate
                    "event_type": "state_transition",
                    "operation_id": operation.operation_id,
                    "step_id": "",
                    "detail": {"from": from_status, "to": to_status},
                    "status": to_status.lower(),
                })

        # Add step events
        for step_id, result in operation.steps.items():
            if result.status in (StepStatus.COMPLETED.value, StepStatus.FAILED.value):
                events.append({
                    "timestamp": operation.updated_at,
                    "event_type": "step_completed" if result.status == StepStatus.COMPLETED.value else "step_failed",
                    "operation_id": operation.operation_id,
                    "step_id": step_id,
                    "detail": {
                        "attempt": 1,
                        "detail": result.detail,
                        "error_code": result.error_code,
                    },
                    "status": result.status.lower(),
                })

        # Sort by timestamp
        events.sort(key=lambda e: e["timestamp"])
        return events

    # -- Renderers ------------------------------------------------------------

    def render_mermaid(self, data: SagaVisualizationData) -> str:
        """Render as Mermaid flowchart."""
        lines = ["```mermaid", "flowchart TD"]

        # Styles
        for status, color in self.STATUS_COLORS.items():
            lines.append(f"    classDef {status.lower()} fill:{color},stroke:#333,stroke-width:2px")
        for engine, color in self.ENGINE_COLORS.items():
            lines.append(f"    classDef {engine.lower()} fill:{color},stroke:#333,stroke-width:1px,color:#fff")

        # Nodes
        for node in data.nodes:
            status_class = node.status.lower().replace("_", "-")
            engine_class = node.engine.lower().replace("-", "-") if node.engine else ""
            classes = [status_class, engine_class, node.node_type]
            class_str = " ".join(c for c in classes if c)
            label = node.label.replace("\n", "<br/>")
            lines.append(f'    {node.node_id}["{label}"]:::{" ".join(classes)}')

        # Edges
        for edge in data.edges:
            style = ""
            if edge.edge_type == "compensation":
                style = "-.->"
            elif edge.edge_type == "depends_on":
                style = "-->"
            else:
                style = "-->"
            label = f'|"{edge.label}"|' if edge.label else ""
            lines.append(f"    {edge.from_node} {style}{label} {edge.to_node}")

        # Add clickable nodes (Mermaid)
        lines.append("")
        lines.append("    click callback node clickHandler")
        lines.append("```")

        return "\n".join(lines)

    def render_graphviz_dot(self, data: SagaVisualizationData) -> str:
        """Render as Graphviz DOT format."""
        lines = [
            "digraph Saga {",
            '    rankdir=LR;',
            '    node [fontname="Arial", fontsize=10];',
            '    edge [fontname="Arial", fontsize=8];',
        ]

        # Nodes
        for node in data.nodes:
            color = self.STATUS_COLORS.get(node.status, "#94a3b8")
            engine_color = self.ENGINE_COLORS.get(node.engine, "#94a3b8") if node.engine else "#94a3b8"
            shape = "box" if node.node_type == "operation" else "ellipse"
            if node.node_type == "compensation":
                shape = "diamond"

            label = node.label.replace("\n", "\\n")
            fillcolor = self.STATUS_COLORS.get(node.status, "#94a3b8")
            style = "filled,rounded"

            lines.append(f'    {node.node_id} [label="{label}", fillcolor="{fillcolor}", style="{style}", shape="{shape}"];')

        # Edges
        for edge in data.edges:
            style = "dashed" if edge.edge_type == "compensation" else "solid"
            color = "#a855f7" if edge.edge_type == "compensation" else "#333"
            label = f' [label="{edge.label}", style="{style}", color="{color}"]' if edge.label else f' [style="{style}", color="{color}"]'
            lines.append(f'    {edge.from_node} -> {edge.to_node}{label};')

        lines.append("}")
        return "\n".join(lines)

    def render_json(self, data: SagaVisualizationData) -> str:
        """Render as JSON."""
        return json.dumps({
            "operation_id": data.operation_id,
            "operation_type": data.operation_type,
            "module_id": data.module_id,
            "status": data.status,
            "nodes": [
                {
                    "id": n.node_id,
                    "label": n.label,
                    "type": n.node_type,
                    "status": n.status,
                    "engine": n.engine,
                    "details": n.details,
                }
                for n in data.nodes
            ],
            "edges": [
                {
                    "from": e.from_node,
                    "to": e.to_node,
                    "type": e.edge_type,
                    "label": e.label,
                }
                for e in data.edges
            ],
            "timeline": data.timeline,
            "metadata": data.metadata,
            "generated_at": data.generated_at,
        }, indent=2, ensure_ascii=False)

    def render_ascii(self, data: SagaVisualizationData) -> str:
        """Render as ASCII diagram."""
        lines = [
            f"Saga Operation: {data.operation_id}",
            f"Type: {data.operation_type} | Module: {data.module_id} | Status: {data.status}",
            "",
            "Steps:",
        ]

        step_nodes = [n for n in data.nodes if n.node_type == "step"]
        for i, node in enumerate(step_nodes):
            prefix = "└──" if i == len(step_nodes) - 1 else "├──"
            status = node.status
            lines.append(f"  {prefix} {node.label} [{status}]")

        if data.edges:
            comp_edges = [e for e in data.edges if e.edge_type == "compensation"]
            if comp_edges:
                lines.append("")
                lines.append("Compensations:")
                for edge in comp_edges:
                    lines.append(f"  ↩ {edge.label}: {edge.from_node} --> {edge.to_node}")

        return "\n".join(lines)

    def render_timeline(self, data: SagaVisualizationData) -> str:
        """Render as chronological timeline."""
        lines = [
            f"Timeline for {data.operation_id}",
            f"Generated: {datetime.fromtimestamp(data.generated_at).isoformat()}",
            "",
        ]

        for event in data.timeline:
            dt = datetime.fromtimestamp(event["timestamp"])
            time_str = dt.strftime("%H:%M:%S.%f")[:-3]
            step_str = f" [{event['step_id']}]" if event.get("step_id") else ""
            lines.append(f"  {time_str} {event['event_type']}{step_str}: {event['detail']}")

        return "\n".join(lines)

    def render(self, data: SagaVisualizationData, format: VisualizationFormat = VisualizationFormat.MERMAID) -> str:
        """Render visualization in specified format."""
        if format == VisualizationFormat.MERMAID:
            return self.render_mermaid(data)
        elif format == VisualizationFormat.GRAPHVIZ_DOT:
            return self.render_graphviz_dot(data)
        elif format == VisualizationFormat.JSON:
            return self.render_json(data)
        elif format == VisualizationFormat.ASCII:
            return self.render_ascii(data)
        elif format == VisualizationFormat.TIMELINE:
            return self.render_timeline(data)
        else:
            return self.render_mermaid(data)


def create_saga_visualizer() -> SagaVisualizer:
    """Factory function to create a SagaVisualizer instance."""
    return SagaVisualizer()


def to_panel_payload(data: SagaVisualizationData) -> dict[str, Any]:
    """Project visualization data to the renderer panel wire contract."""
    return {
        "operation_id": data.operation_id,
        "operation_type": data.operation_type,
        "module_id": data.module_id,
        "status": data.status,
        "nodes": [
            {
                "id": node.node_id,
                "label": node.label,
                "type": node.node_type,
                "status": node.status,
                "engine": node.engine,
                "details": dict(node.details),
            }
            for node in data.nodes
        ],
        "edges": [
            {
                "from": edge.from_node,
                "to": edge.to_node,
                "type": edge.edge_type,
                "label": edge.label,
            }
            for edge in data.edges
        ],
        "timeline": list(data.timeline),
        "metadata": dict(data.metadata),
        "generated_at": data.generated_at,
    }


__all__ = [
    "VisualizationFormat",
    "SagaNode",
    "SagaEdge",
    "SagaTimelineEvent",
    "SagaVisualizationData",
    "SagaVisualizer",
    "create_saga_visualizer",
    "to_panel_payload",
]