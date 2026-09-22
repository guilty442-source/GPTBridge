"""Dependency Graph Builders — cross-language dependency graph construction.

Builds dependency graphs for all GPTBridge languages:
    - Python: import graph (stdlib, third-party, intra-package)
    - TypeScript: package/bundle graph (package.json deps, imports)
    - C/C++: include/link graph (#include, link targets)
    - C#: project/NuGet reference graph (none in current codebase)
    - SQL: migration/schema dependency graph (migration ordering, FK refs)

Each graph is a directed graph where edges represent "depends on".
Nodes are classified by language and dependency type.

The graphs feed the dependency classifier (dep_classifier.py) and the
build impact analyzer (build_impact.py).

Codex basis:
    A205 — API boundary (graphs respect language boundaries)
    A204/A220 — C ABI (C/C++ graph respects ABI boundary)
    A198 — third-party boundary (third-party deps are marked)
    A211 — six-language canonical roles
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class DependencyType(str, Enum):
    """Type of dependency edge."""
    IMPORT = "import"           # Python import
    INCLUDE = "include"         # C/C++ #include
    PACKAGE = "package"         # TS/JS package.json dependency
    BUNDLE = "bundle"           # TS/JS import in source
    PROJECT_REF = "project_ref" # C# project reference
    NUGET = "nuget"             # C# NuGet package
    MIGRATION = "migration"     # SQL migration dependency
    SCHEMA_REF = "schema_ref"   # SQL schema/FK reference
    LINK = "link"               # C/C++ link target
    BUILD = "build"             # cross-language build dependency


class NodeLanguage(str, Enum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    C = "c"
    CPP = "cpp"
    CSHARP = "csharp"
    SQL = "sql"
    BUILD = "build"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DepNode:
    """A node in the dependency graph."""
    node_id: str          # unique identifier (e.g. file path or package name)
    name: str             # human-readable name
    language: NodeLanguage
    is_third_party: bool  # True if this is a third-party dependency
    is_stdlib: bool       # True if this is a stdlib/standard library
    path: str = ""        # file path if applicable


@dataclass(frozen=True)
class DepEdge:
    """A directed edge in the dependency graph: source depends on target."""
    source: str   # node_id
    target: str   # node_id
    dep_type: DependencyType
    is_transitive: bool = False  # True if this is a transitive dependency


@dataclass
class DependencyGraph:
    """A directed dependency graph."""
    nodes: dict[str, DepNode] = field(default_factory=dict)
    edges: list[DepEdge] = field(default_factory=list)
    # Y14: lazily built adjacency + memoized transitive closures, invalidated
    # by a version counter bumped on every mutation.
    _version: int = field(default=0, init=False, repr=False)
    _built_version: int = field(default=-1, init=False, repr=False)
    _fwd: dict[str, list[str]] | None = field(default=None, init=False, repr=False)
    _rev: dict[str, list[str]] | None = field(default=None, init=False, repr=False)
    _closure_deps: dict[int, dict[str, frozenset[str]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _closure_rev: dict[int, dict[str, frozenset[str]]] = field(
        default_factory=dict, init=False, repr=False
    )

    def add_node(self, node: DepNode) -> None:
        if node.node_id not in self.nodes:
            self.nodes[node.node_id] = node
            self._version += 1

    def add_edge(self, edge: DepEdge) -> None:
        self.edges.append(edge)
        self._version += 1

    def _adjacency(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        if self._fwd is None or self._rev is None:
            fwd: dict[str, list[str]] = {}
            rev: dict[str, list[str]] = {}
            for e in self.edges:
                fwd.setdefault(e.source, []).append(e.target)
                rev.setdefault(e.target, []).append(e.source)
            self._fwd, self._rev = fwd, rev
        return self._fwd, self._rev

    def _invalidate(self) -> None:
        self._fwd = None
        self._rev = None
        self._closure_deps.clear()
        self._closure_rev.clear()

    def dependents(self, node_id: str) -> list[str]:
        """Return node_ids that depend on the given node (reverse edges)."""
        self._refresh()
        _, rev = self._adjacency()
        return list(rev.get(node_id, ()))

    def dependencies(self, node_id: str) -> list[str]:
        """Return node_ids that the given node depends on (forward edges)."""
        self._refresh()
        fwd, _ = self._adjacency()
        return list(fwd.get(node_id, ()))

    def transitive_dependencies(self, node_id: str) -> set[str]:
        """Return all transitive dependencies of a node (memoized per version)."""
        return set(self._closure(node_id, self._closure_deps, forward=True))

    def transitive_dependents(self, node_id: str) -> set[str]:
        """Return all transitive dependents of a node (reverse, memoized)."""
        return set(self._closure(node_id, self._closure_rev, forward=False))

    def _edge_version_stale(self) -> bool:
        # Mutations bump _version; adjacency rebuilt lazily on next access.
        return self._built_version != self._version

    def _closure(
        self, node_id: str, cache: dict[int, dict[str, frozenset[str]]], *, forward: bool
    ) -> frozenset[str]:
        self._refresh()
        bucket = cache.setdefault(self._version, {})
        hit = bucket.get(node_id)
        if hit is not None:
            return hit
        fwd, rev = self._adjacency()
        adj = fwd if forward else rev
        result: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            for dep in adj.get(current, ()):
                if dep not in result:
                    result.add(dep)
                    stack.append(dep)
        frozen = frozenset(result)
        bucket[node_id] = frozen
        return frozen

    def _refresh(self) -> None:
        if self._edge_version_stale():
            self._invalidate()
            self._built_version = self._version

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {
                nid: {
                    "name": n.name,
                    "language": n.language.value,
                    "is_third_party": n.is_third_party,
                    "is_stdlib": n.is_stdlib,
                    "path": n.path,
                }
                for nid, n in self.nodes.items()
            },
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "dep_type": e.dep_type.value,
                    "is_transitive": e.is_transitive,
                }
                for e in self.edges
            ],
        }


# ---------------------------------------------------------------------------
# Python Import Graph
# ---------------------------------------------------------------------------

# Python stdlib modules (subset — the ones commonly imported)
_PYTHON_STDLIB_PREFIXES = frozenset({
    "abc", "argparse", "ast", "asyncio", "base64", "bisect", "calendar",
    "collections", "concurrent", "configparser", "contextlib", "copy",
    "csv", "ctypes", "dataclasses", "datetime", "decimal", "difflib",
    "enum", "errno", "faulthandler", "fnmatch", "functools", "gc",
    "getpass", "glob", "hashlib", "heapq", "hmac", "html", "http",
    "importlib", "inspect", "io", "itertools", "json", "logging",
    "math", "mimetypes", "multiprocessing", "operator", "os", "pathlib",
    "pickle", "platform", "pprint", "queue", "random", "re", "secrets",
    "shutil", "signal", "socket", "sqlite3", "ssl", "statistics",
    "string", "struct", "subprocess", "sys", "tarfile", "tempfile",
    "textwrap", "threading", "time", "traceback", "tracemalloc",
    "typing", "unittest", "urllib", "uuid", "warnings", "weakref",
    "xml", "zipfile", "zlib", "cProfile", "pstats", "io",
})


def _is_stdlib(module_name: str) -> bool:
    """Check if a module name is a stdlib module."""
    top = module_name.split(".")[0]
    return top in _PYTHON_STDLIB_PREFIXES


def _is_third_party_python(module_name: str, local_packages: set[str]) -> bool:
    """Check if a module name is a third-party package."""
    top = module_name.split(".")[0]
    if top in local_packages:
        return False
    if _is_stdlib(module_name):
        return False
    # If not stdlib and not local, it's third-party
    return True


def build_python_import_graph(
    root: Path,
    *,
    local_packages: set[str] | None = None,
) -> DependencyGraph:
    """Build a Python import graph from .py files under root.

    Scans all .py files, parses import statements, and creates edges
    for each import. Local packages are treated as internal nodes;
    stdlib and third-party are marked accordingly.
    """
    graph = DependencyGraph()
    local_packages = local_packages or set()

    for py_file in root.rglob("*.py"):
        # Skip common non-source directories
        parts = py_file.parts
        if any(p in {".venv", "__pycache__", "node_modules", ".git", "dist", "build"} for p in parts):
            continue

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        # Create node for this file
        rel_path = str(py_file.relative_to(root)).replace("\\", "/")
        file_node_id = f"py:{rel_path}"
        graph.add_node(DepNode(
            node_id=file_node_id,
            name=rel_path,
            language=NodeLanguage.PYTHON,
            is_third_party=False,
            is_stdlib=False,
            path=str(py_file),
        ))

        # Extract imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _add_python_import_edge(
                        graph, file_node_id, alias.name,
                        local_packages, root,
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    _add_python_import_edge(
                        graph, file_node_id, node.module,
                        local_packages, root,
                    )

    return graph


def _add_python_import_edge(
    graph: DependencyGraph,
    source_id: str,
    module_name: str,
    local_packages: set[str],
    root: Path,
) -> None:
    """Add an edge for a Python import."""
    top = module_name.split(".")[0]
    is_std = _is_stdlib(module_name)
    is_third = _is_third_party_python(module_name, local_packages)

    target_id = f"py:pkg:{top}"
    if target_id not in graph.nodes:
        graph.add_node(DepNode(
            node_id=target_id,
            name=top,
            language=NodeLanguage.PYTHON,
            is_third_party=is_third,
            is_stdlib=is_std,
        ))

    graph.add_edge(DepEdge(
        source=source_id,
        target=target_id,
        dep_type=DependencyType.IMPORT,
    ))


# ---------------------------------------------------------------------------
# TypeScript Package Graph
# ---------------------------------------------------------------------------

def build_typescript_package_graph(
    package_json_path: Path,
) -> DependencyGraph:
    """Build a TypeScript package graph from package.json.

    Parses dependencies and devDependencies, creating nodes for each
    package and edges from the project root to each dependency.
    """
    graph = DependencyGraph()

    if not package_json_path.exists():
        return graph

    data = json.loads(package_json_path.read_text(encoding="utf-8"))

    # Root node
    root_id = "ts:project"
    graph.add_node(DepNode(
        node_id=root_id,
        name=data.get("name", "unknown"),
        language=NodeLanguage.TYPESCRIPT,
        is_third_party=False,
        is_stdlib=False,
        path=str(package_json_path),
    ))

    deps = data.get("dependencies", {})
    dev_deps = data.get("devDependencies", {})

    for pkg_name, version_spec in deps.items():
        node_id = f"ts:pkg:{pkg_name}"
        graph.add_node(DepNode(
            node_id=node_id,
            name=pkg_name,
            language=NodeLanguage.TYPESCRIPT,
            is_third_party=True,
            is_stdlib=False,
        ))
        graph.add_edge(DepEdge(
            source=root_id,
            target=node_id,
            dep_type=DependencyType.PACKAGE,
        ))

    for pkg_name, version_spec in dev_deps.items():
        node_id = f"ts:pkg:{pkg_name}"
        graph.add_node(DepNode(
            node_id=node_id,
            name=pkg_name,
            language=NodeLanguage.TYPESCRIPT,
            is_third_party=True,
            is_stdlib=False,
        ))
        graph.add_edge(DepEdge(
            source=root_id,
            target=node_id,
            dep_type=DependencyType.PACKAGE,
        ))

    return graph


# ---------------------------------------------------------------------------
# C/C++ Include Graph
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(
    r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE
)


def build_cc_include_graph(
    native_root: Path,
) -> DependencyGraph:
    """Build a C/C++ include graph from .c, .cpp, .h, .hpp files.

    Scans all source files under native_root, parses #include directives,
    and creates edges. System includes (angle brackets) are marked as
    stdlib; local includes (quotes) are internal.
    """
    graph = DependencyGraph()

    for src_file in native_root.rglob("*"):
        if src_file.suffix not in {".c", ".cpp", ".h", ".hpp"}:
            continue

        try:
            source = src_file.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        rel_path = str(src_file.relative_to(native_root)).replace("\\", "/")
        file_node_id = f"cc:{rel_path}"
        graph.add_node(DepNode(
            node_id=file_node_id,
            name=rel_path,
            language=NodeLanguage.CPP if src_file.suffix in {".cpp", ".hpp"} else NodeLanguage.C,
            is_third_party=False,
            is_stdlib=False,
            path=str(src_file),
        ))

        for match in _INCLUDE_RE.finditer(source):
            include_path = match.group(1)
            is_system = ">" in match.group(0) or match.group(0).find("<") >= 0

            # Determine if it's a system include or local
            is_std = is_system and not include_path.startswith("gptbridge")
            is_local = not is_system or include_path.startswith("gptbridge")

            target_id = f"cc:inc:{include_path}"
            if target_id not in graph.nodes:
                graph.add_node(DepNode(
                    node_id=target_id,
                    name=include_path,
                    language=NodeLanguage.CPP,
                    is_third_party=False,
                    is_stdlib=is_std,
                ))

            graph.add_edge(DepEdge(
                source=file_node_id,
                target=target_id,
                dep_type=DependencyType.INCLUDE,
            ))

    return graph


# ---------------------------------------------------------------------------
# C# Project Graph (empty in current codebase)
# ---------------------------------------------------------------------------

def build_csharp_project_graph(
    root: Path,
) -> DependencyGraph:
    """Build a C# project/NuGet reference graph.

    Scans for .csproj files and parses ProjectReference and PackageReference.
    Returns an empty graph if no .csproj files exist.
    """
    graph = DependencyGraph()

    csproj_files = list(root.rglob("*.csproj"))
    if not csproj_files:
        return graph

    # Parse .csproj XML for references
    for csproj in csproj_files:
        try:
            content = csproj.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        rel_path = str(csproj.relative_to(root)).replace("\\", "/")
        proj_id = f"cs:{rel_path}"
        graph.add_node(DepNode(
            node_id=proj_id,
            name=csproj.stem,
            language=NodeLanguage.CSHARP,
            is_third_party=False,
            is_stdlib=False,
            path=str(csproj),
        ))

        # Simple regex-based parsing (avoid xml.etree for robustness)
        for match in re.finditer(r'<PackageReference\s+Include="([^"]+)"\s+Version="([^"]+)"', content):
            pkg_name = match.group(1)
            pkg_id = f"cs:nuget:{pkg_name}"
            graph.add_node(DepNode(
                node_id=pkg_id,
                name=pkg_name,
                language=NodeLanguage.CSHARP,
                is_third_party=True,
                is_stdlib=False,
            ))
            graph.add_edge(DepEdge(
                source=proj_id,
                target=pkg_id,
                dep_type=DependencyType.NUGET,
            ))

        for match in re.finditer(r'<ProjectReference\s+Include="([^"]+)"', content):
            ref_path = match.group(1)
            ref_id = f"cs:{ref_path}"
            graph.add_node(DepNode(
                node_id=ref_id,
                name=Path(ref_path).stem,
                language=NodeLanguage.CSHARP,
                is_third_party=False,
                is_stdlib=False,
            ))
            graph.add_edge(DepEdge(
                source=proj_id,
                target=ref_id,
                dep_type=DependencyType.PROJECT_REF,
            ))

    return graph


# ---------------------------------------------------------------------------
# SQL Migration Dependency Graph
# ---------------------------------------------------------------------------

_MIGRATION_NUM_RE = re.compile(r"^(\d+)_")
_FK_RE = re.compile(
    r"REFERENCES\s+(?:gptbridge_\w+\.)?(\w+)", re.IGNORECASE
)
_SCHEMA_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:gptbridge_\w+\.)?(\w+)",
    re.IGNORECASE,
)


def build_sql_migration_graph(
    migrations_dir: Path,
) -> DependencyGraph:
    """Build a SQL migration dependency graph.

    Creates nodes for each migration file and edges based on:
    - Migration number ordering (sequential dependency)
    - Foreign key references between tables
    """
    graph = DependencyGraph()

    sql_files = sorted(migrations_dir.glob("*.sql"))
    prev_migration_id: str | None = None

    for sql_file in sql_files:
        match = _MIGRATION_NUM_RE.match(sql_file.name)
        if not match:
            continue
        migration_num = int(match.group(1))

        migration_id = f"sql:mig:{migration_num:03d}"
        graph.add_node(DepNode(
            node_id=migration_id,
            name=sql_file.name,
            language=NodeLanguage.SQL,
            is_third_party=False,
            is_stdlib=False,
            path=str(sql_file),
        ))

        # Sequential dependency: each migration depends on the previous
        if prev_migration_id is not None:
            graph.add_edge(DepEdge(
                source=migration_id,
                target=prev_migration_id,
                dep_type=DependencyType.MIGRATION,
            ))

        # Parse for table references
        try:
            content = sql_file.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            content = ""

        # Create nodes for tables created in this migration
        for table_match in _SCHEMA_RE.finditer(content):
            table_name = table_match.group(1)
            table_id = f"sql:table:{table_name}"
            if table_id not in graph.nodes:
                graph.add_node(DepNode(
                    node_id=table_id,
                    name=table_name,
                    language=NodeLanguage.SQL,
                    is_third_party=False,
                    is_stdlib=False,
                ))
            # Migration creates this table
            graph.add_edge(DepEdge(
                source=migration_id,
                target=table_id,
                dep_type=DependencyType.SCHEMA_REF,
            ))

        # FK references: this migration's tables reference other tables
        for fk_match in _FK_RE.finditer(content):
            ref_table = fk_match.group(1)
            ref_id = f"sql:table:{ref_table}"
            if ref_id not in graph.nodes:
                graph.add_node(DepNode(
                    node_id=ref_id,
                    name=ref_table,
                    language=NodeLanguage.SQL,
                    is_third_party=False,
                    is_stdlib=False,
                ))
            # Migration depends on the referenced table
            graph.add_edge(DepEdge(
                source=migration_id,
                target=ref_id,
                dep_type=DependencyType.SCHEMA_REF,
            ))

        prev_migration_id = migration_id

    return graph


# ---------------------------------------------------------------------------
# Cross-Language Build Graph
# ---------------------------------------------------------------------------

def build_cross_language_build_graph(
    graphs: dict[str, DependencyGraph],
) -> DependencyGraph:
    """Build a cross-language source→artifact build graph.

    Combines per-language graphs and adds cross-language build edges:
    - Python → C++ (pybind11 binding depends on native core)
    - TypeScript → Python (Electron main calls Python)
    - SQL → Python (Python database layer depends on SQL schema)
    """
    combined = DependencyGraph()

    # Merge all nodes and edges
    for graph in graphs.values():
        for node_id, node in graph.nodes.items():
            combined.add_node(node)
        for edge in graph.edges:
            combined.add_edge(edge)

    # Cross-language build edges (specific to GPTBridge)
    # Python binding depends on C++ native core
    # These are identified by convention in the build system
    for node_id, node in combined.nodes.items():
        if node.language == NodeLanguage.PYTHON and "native" in node.name:
            # Python native adapter depends on C++ core
            for cc_id, cc_node in combined.nodes.items():
                if cc_node.language == NodeLanguage.CPP and cc_node.name.endswith((".cpp", ".hpp")):
                    combined.add_edge(DepEdge(
                        source=node_id,
                        target=cc_id,
                        dep_type=DependencyType.BUILD,
                    ))

    return combined


# ---------------------------------------------------------------------------
# Full Graph Builder
# ---------------------------------------------------------------------------

def build_all_dependency_graphs(
    project_root: Path,
) -> dict[str, DependencyGraph]:
    """Build all dependency graphs for the project.

    Returns a dict mapping graph name to DependencyGraph:
        - "python": Python import graph
        - "typescript": TypeScript package graph
        - "cc": C/C++ include graph
        - "csharp": C# project graph
        - "sql": SQL migration graph
        - "cross_language": combined cross-language graph
    """
    graphs: dict[str, DependencyGraph] = {}

    # Python import graph
    # Local packages: shared_layer, core_system, governance_rule
    local_packages = {"shared_layer", "core_system", "governance_rule"}
    graphs["python"] = build_python_import_graph(
        project_root, local_packages=local_packages,
    )

    # TypeScript package graph
    ts_pkg = project_root / "main-system" / "package.json"
    graphs["typescript"] = build_typescript_package_graph(ts_pkg)

    # C/C++ include graph
    native_root = project_root / "native"
    if native_root.exists():
        graphs["cc"] = build_cc_include_graph(native_root)
    else:
        graphs["cc"] = DependencyGraph()

    # C# project graph
    graphs["csharp"] = build_csharp_project_graph(project_root)

    # SQL migration graph
    migrations_dir = project_root / "shared-layer" / "migrations"
    if migrations_dir.exists():
        graphs["sql"] = build_sql_migration_graph(migrations_dir)
    else:
        graphs["sql"] = DependencyGraph()

    # Cross-language build graph
    graphs["cross_language"] = build_cross_language_build_graph(graphs)

    return graphs


__all__ = [
    "DependencyType",
    "NodeLanguage",
    "DepNode",
    "DepEdge",
    "DependencyGraph",
    "build_python_import_graph",
    "build_typescript_package_graph",
    "build_cc_include_graph",
    "build_csharp_project_graph",
    "build_sql_migration_graph",
    "build_cross_language_build_graph",
    "build_all_dependency_graphs",
]
