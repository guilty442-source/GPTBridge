"""Contract Dependency Graph Generator.

Analyzes the canonical type registry and generates dependency graphs
showing how types depend on each other. Reduces manual contract maintenance
by making dependencies explicit and automatically generated.

Output formats:
- Mermaid (for documentation)
- GraphViz DOT (for tooling)
- JSON (for tooling integration)
- ASCII (for quick terminal viewing)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .registry import CanonicalTypeRegistry, DEFAULT_REGISTRY
from .types import (
    CanonicalKind,
    FieldSpec,
    Presence,
    TypeSpec,
)
from .types import EnumSpec, EnumValue, IdentifierSpec, TypedBufferSpec


class DependencyKind(Enum):
    """Types of dependencies between types."""
    FIELD = "field"           # Field in object type references another type
    ELEMENT = "element"       # List element type
    ENUM_VALUE = "enum_value" # Enum value reference
    BUFFER = "buffer"         # Typed buffer dtype
    IDENTIFIER = "identifier" # Identifier spec
    NESTED = "nested"         # Nested object type
    EXTENDS = "extends"       # Type extension/inheritance


@dataclass(frozen=True)
class DependencyEdge:
    """A dependency edge in the graph."""
    from_type: str
    to_type: str
    kind: DependencyKind
    detail: str = ""


@dataclass(frozen=True)
class GraphNode:
    """A node in the dependency graph."""
    name: str
    kind: str
    fields: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DependencyGraph:
    """Complete dependency graph."""
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[DependencyEdge] = field(default_factory=list)
    generated_at: float = field(default_factory=lambda: datetime.now().timestamp())
    registry_version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


class DependencyGraphBuilder:
    """Builds dependency graphs from the canonical type registry."""

    def __init__(self, registry: CanonicalTypeRegistry | None = None) -> None:
        self.registry = registry or DEFAULT_REGISTRY

    def build(self) -> DependencyGraph:
        """Build the complete dependency graph."""
        nodes = {}
        edges = []

        # Build nodes from registry
        for name, spec in self.registry:
            nodes[name] = self._create_node(name, spec)

        # Build edges from dependencies
        for name, spec in self.registry:
            edges.extend(self._extract_dependencies(name, spec))

        return DependencyGraph(
            nodes=nodes,
            edges=edges,
            registry_version=1,  # TODO: get from registry
        )

    def _create_node(self, name: str, spec: TypeSpec) -> dict:
        """Create a graph node from a TypeSpec."""
        fields = []
        metadata = {
            "kind": spec.kind.value,
            "presence": spec.presence.value,
        }

        if spec.kind in (CanonicalKind.LIST, CanonicalKind.OBJECT):
            if spec.element:
                fields.append(f"element:{spec.element.name}")
                metadata["element_type"] = spec.element.name

        if spec.fields:
            for field in spec.fields:
                fields.append(f"field:{field.name}:{field.spec.name}")

        if spec.enum:
            for val in spec.enum.values:
                metadata.setdefault("enum_values", []).append(val.name)

        if spec.buffer:
            fields.append(f"buffer:{spec.buffer.dtype.name}")
            metadata["buffer_dtype"] = spec.buffer.dtype.name

        if spec.identifier:
            metadata["identifier"] = {
                "max_length": spec.identifier.max_length,
                "pattern": spec.identifier.pattern,
            }

        return {
            "name": name,
            "kind": spec.kind.value,
            "fields": fields,
            "metadata": metadata,
        }

    def _extract_dependencies(self, name: str, spec: TypeSpec) -> list[DependencyEdge]:
        """Extract dependencies from a TypeSpec."""
        deps = []

        # Element type dependency (LIST)
        if spec.kind == CanonicalKind.LIST and spec.element:
            deps.append(DependencyEdge(
                from_type=name,
                to_type=spec.element.name,
                kind=DependencyKind.ELEMENT,
                detail=f"list element type",
            ))

        # Field type dependencies (OBJECT)
        if spec.kind == CanonicalKind.OBJECT and spec.fields:
            for field in spec.fields:
                deps.append(DependencyEdge(
                    from_type=name,
                    to_type=field.spec.name,
                    kind=DependencyKind.FIELD,
                    detail=f"field '{field.name}'",
                ))

        # Buffer dtype dependency
        if spec.kind == CanonicalKind.TYPED_BUFFER and spec.buffer:
            deps.append(DependencyEdge(
                from_type=name,
                to_type=spec.buffer.dtype.name,
                kind=DependencyKind.BUFFER,
                detail=f"buffer dtype {spec.buffer.dtype.name}",
            ))

        # Enum values (though these are inline, could reference other types)
        if spec.kind == CanonicalKind.ENUM and spec.enum:
            for val in spec.enum.values:
                # Enums are inline, no external refs
                pass

        # Identifier spec (could have pattern dependencies)
        if spec.identifier:
            # No external refs typically
            pass

        # Nested types (recursive)
        # Recursively extract from nested types
        if spec.element:
            deps.extend(self._extract_dependencies(
                spec.element.name, spec.element
            ))

        for field in spec.fields:
            deps.extend(self._extract_dependencies(
                field.spec.name, field.spec
            ))

        return deps


class GraphRenderer:
    """Renders dependency graphs in various formats."""

    def __init__(self, graph: DependencyGraph) -> None:
        self.graph = graph

    def render_mermaid(self) -> str:
        """Render as Mermaid flowchart."""
        lines = ["```mermaid", "flowchart TD"]

        # Styles
        lines.append("    classDef type fill:#e1f5fe,stroke:#0277bd,stroke-width:2px")
        lines.append("    classDef object fill:#fff3e0,stroke:#e65100,stroke-width:2px")
        lines.append("    classDef list fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px")
        lines.append("    classDef enum fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px")
        lines.append("    classDef primitive fill:#f5f5f5,stroke:#757575,stroke-width:1px")
        lines.append("    classDef buffer fill:#fce4ec,stroke:#c2185b,stroke-width:2px")

        # Nodes
        for name, node in self.graph.nodes.items():
            kind = node.get("kind", "type")
            fields_str = ", ".join(node.get("fields", []))
            label = f"{name}"
            if fields_str:
                label += f"\\n{fields_str}"
            label = label.replace("\n", "<br/>")

            klass = node.get("kind", "type")
            lines.append(f'    {name}["{label}"]:::{kind.lower()}')

        # Edges - would need edge data
        # For now, just show nodes

        lines.append("```")
        return "\n".join(lines)

    def render_graphviz(self) -> str:
        """Render as GraphViz DOT."""
        lines = [
            "digraph ContractDeps {",
            '    rankdir=LR;',
            '    node [fontname="Arial", fontsize=10];',
            '    edge [fontname="Arial", fontsize=8];',
        ]

        # Nodes
        for name, node in self.graph.nodes.items():
            kind = node.get("kind", "type")
            color = self._color_for_kind(node.get("kind", "type"))
            label = name
            lines.append(f'    "{node["name"]}" [label="{label}", fillcolor="{color}", style="filled,rounded", shape="box"];')

        lines.append("}")
        return "\n".join(lines)

    def _color_for_kind(self, kind: str) -> str:
        colors = {
            "I32": "#e1f5fe", "U32": "#e1f5fe", "I64": "#e1f5fe", "U64": "#e1f5fe",
            "F32": "#e1f5fe", "F64": "#e1f5fe",
            "BOOL": "#fff3e0", "STRING": "#fff3e0", "BYTES": "#fff3e0",
            "TIMESTAMP_UTC": "#e8f5e9", "DURATION": "#e8f5e9", "DEADLINE": "#e8f5e9",
            "ENUM": "#fff3e0",
            "IDENTIFIER": "#fff3e0",
            "LIST": "#f3e5f5",
            "OBJECT": "#fce4ec",
            "TYPED_BUFFER": "#fce4ec",
            "type": "#f5f5f5",
        }
        return colors.get(kind, "#f5f5f5")

    def render_json(self) -> str:
        """Render as JSON."""
        return json.dumps({
            "nodes": self.graph.nodes,
            "edges": [
                {"from": e.from_type, "to": e.to_type, "kind": e.kind, "detail": e.detail}
                for e in self.graph.edges
            ],
            "metadata": self.graph.metadata,
            "generated_at": self.graph.generated_at,
            "registry_version": self.graph.registry_version,
        }, indent=2, ensure_ascii=False)

    def render_ascii(self) -> str:
        """Render as ASCII diagram."""
        lines = [
            "Contract Dependency Graph",
            "=" * 50,
            f"Generated: {datetime.fromtimestamp(self.graph.generated_at).isoformat()}",
            f"Registry Version: {self.graph.registry_version}",
            "",
            "Nodes:",
        ]

        for name, node in self.graph.nodes.items():
            kind = node.get("kind", "type")
            fields = node.get("fields", [])
            lines.append(f"  {name} ({kind})")
            for field in fields:
                lines.append(f"    └─ {field}")

        if self.graph.edges:
            lines.append("")
            lines.append("Dependencies:")
            for edge in self.graph.edges:
                lines.append(f"  {edge.from_type} --{edge.kind}--> {edge.to_type}")
                if edge.detail:
                    lines.append(f"    ({edge.detail})")

        return "\n".join(lines)

    def render(self, format: str = "ascii") -> str:
        """Render in specified format."""
        if format == "mermaid":
            return self.render_mermaid()
        elif format == "graphviz":
            return self.render_graphviz()
        elif format == "json":
            return self.render_json()
        elif format == "ascii":
            return self.render_ascii()
        else:
            return self.render_ascii()


def build_dependency_graph(registry: CanonicalTypeRegistry | None = None) -> DependencyGraph:
    """Build dependency graph from registry."""
    builder = DependencyGraphBuilder(registry)
    return builder.build()


def render_dependency_graph(
    graph: DependencyGraph,
    format: str = "ascii"
) -> str:
    """Render dependency graph in specified format."""
    renderer = GraphRenderer(graph)
    return renderer.render(format)


def main() -> None:
    """CLI entry point."""
    import sys

    format = "ascii"
    if len(sys.argv) > 1:
        format = sys.argv[1]

    graph = build_dependency_graph()
    output = render_dependency_graph(build_dependency_graph(), format)
    print(output)


if __name__ == "__main__":
    main()


__all__ = [
    "DependencyKind",
    "DependencyEdge",
    "GraphNode",
    "DependencyGraph",
    "DependencyGraphBuilder",
    "GraphRenderer",
    "build_dependency_graph",
    "render_dependency_graph",
    "main",
]
