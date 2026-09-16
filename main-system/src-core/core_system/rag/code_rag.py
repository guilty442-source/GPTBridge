"""Git Diff Code RAG — 增量代碼索引。

從 Git diff → changed files → AST incremental analysis →
dependency graph → affected symbols → Qdrant incremental update.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Windows no-window policy for subprocess
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from .version_control import ChunkManager, DiffResult, VersionController, ResourceState

_logger = logging.getLogger("gptbridge.rag.code_rag")

_HUNK_HEADER_RE = re.compile(r"@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@")


@dataclass(frozen=True)
class CodeSymbol:
    """Code symbol extracted from AST."""
    symbol_id: str
    name: str
    kind: str              # function, class, method, variable, import
    qualified_name: str    # module.Class.method
    file_path: str
    line_start: int
    line_end: int
    content_hash: str
    signature: str
    docstring: Optional[str] = None
    dependencies: list[str] = field(default_factory=list)  # Symbol IDs this depends on
    dependents: list[str] = field(default_factory=list)    # Symbol IDs that depend on this


@dataclass(frozen=True)
class FileChange:
    """Represents a changed file from git diff."""
    file_path: str
    change_type: str       # added, modified, deleted, renamed
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    added_lines: int = 0
    removed_lines: int = 0


@dataclass(frozen=True)
class CodeChangeSet:
    """Complete change set from a commit."""
    commit_hash: str
    commit_message: str
    author: str
    timestamp: str
    files: list[FileChange]
    symbols_added: list[CodeSymbol]
    symbols_modified: list[CodeSymbol]
    symbols_deleted: list[CodeSymbol]
    affected_symbols: list[CodeSymbol]  # Including transitive dependents


class GitDiffExtractor:
    """Extracts file changes from git diff."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def get_changes_between(self, base_commit: str, head_commit: str) -> list[FileChange]:
        """Get file changes between two commits."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-status", base_commit, head_commit],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
                creationflags=CREATE_NO_WINDOW,
            )
            changes = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("\t")
                status = parts[0]
                if status.startswith("R"):  # Renamed
                    old_path = parts[1]
                    new_path = parts[2]
                    changes.append(FileChange(
                        file_path=new_path,
                        change_type="renamed",
                        old_path=old_path,
                        new_path=new_path,
                    ))
                elif status == "A":  # Added
                    changes.append(FileChange(
                        file_path=parts[1],
                        change_type="added",
                    ))
                elif status == "M":  # Modified
                    changes.append(FileChange(
                        file_path=parts[1],
                        change_type="modified",
                    ))
                elif status == "D":  # Deleted
                    changes.append(FileChange(
                        file_path=parts[1],
                        change_type="deleted",
                    ))
            return changes
        except subprocess.CalledProcessError as e:
            _logger.error("GitDiffExtractor: git diff failed: %s", e)
            return []

    def get_file_diff(self, base_commit: str, head_commit: str, file_path: str) -> str:
        """Get detailed diff for a specific file."""
        try:
            result = subprocess.run(
                ["git", "diff", base_commit, head_commit, "--", file_path],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
                creationflags=CREATE_NO_WINDOW,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            _logger.warning("GitDiffExtractor: git diff for %s failed: %s", file_path, e)
            return ""

    def get_changed_lines(self, base_commit: str, head_commit: str, file_path: str) -> tuple[set[int], set[int]]:
        """Get added and removed line numbers for a file."""
        diff = self.get_file_diff(base_commit, head_commit, file_path)
        added = set()
        removed = set()
        current_old = 0
        current_new = 0
        for line in diff.split("\n"):
            if line.startswith("@@"):
                # Parse @@ -old_start,old_count +new_start,new_count @@
                match = _HUNK_HEADER_RE.match(line)
                if match:
                    old_start = int(match.group(1))
                    old_count = int(match.group(2)) if match.group(2) else 1
                    new_start = int(match.group(3))
                    new_count = int(match.group(4)) if match.group(4) else 1
                    current_old = old_start
                    current_new = new_start
            elif line.startswith("-") and not line.startswith("---"):
                removed.add(current_old)
                current_old += 1
            elif line.startswith("+") and not line.startswith("+++"):
                added.add(current_new)
                current_new += 1
            else:
                current_old += 1
                current_new += 1
        return added, removed


class PythonASTAnalyzer:
    """Extracts symbols and dependencies from Python AST."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._symbol_cache: dict[str, list[CodeSymbol]] = {}

    def analyze_file(self, file_path: Path) -> list[CodeSymbol]:
        """Extract all symbols from a Python file."""
        cache_key = str(file_path.relative_to(self.project_root))
        if cache_key in self._symbol_cache:
            return self._symbol_cache[cache_key]

        try:
            content = file_path.read_text(encoding="utf-8")
            tree = ast.parse(content)
        except (SyntaxError, UnicodeDecodeError, OSError) as e:
            _logger.warning("PythonASTAnalyzer: failed to parse %s: %s", file_path, e)
            return []

        symbols = []
        visitor = _SymbolVisitor(
            file_path=str(file_path.relative_to(self.project_root)),
            content=content,
            project_root=str(self.project_root),
        )
        visitor.visit(tree)
        symbols = visitor.symbols

        # Build dependency graph
        self._build_dependencies(symbols)

        self._symbol_cache[cache_key] = symbols
        return symbols

    def _build_dependencies(self, symbols: list[CodeSymbol]) -> None:
        """Build dependency graph between symbols."""
        symbol_by_name = {s.qualified_name: s for s in symbols}
        for symbol in symbols:
            for dep_name in symbol.dependencies:
                if dep_name in symbol_by_name:
                    symbol_by_name[dep_name].dependents.append(symbol.symbol_id)

    def invalidate_cache(self, file_path: Path) -> None:
        """Invalidate cache for a file."""
        cache_key = str(file_path.relative_to(self.project_root))
        self._symbol_cache.pop(cache_key, None)

    def get_symbols_for_files(self, file_paths: list[Path]) -> list[CodeSymbol]:
        """Get all symbols from multiple files."""
        all_symbols = []
        for fp in file_paths:
            all_symbols.extend(self.analyze_file(fp))
        return all_symbols


class _SymbolVisitor(ast.NodeVisitor):
    """AST visitor that extracts symbols and their dependencies."""

    def __init__(self, file_path: str, content: str, project_root: str) -> None:
        self.file_path = file_path
        self.content = content
        self.project_root = project_root
        self.symbols: list[CodeSymbol] = []
        self._current_class: Optional[str] = None
        self._imports: dict[str, str] = {}  # alias -> full_name

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._imports[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            full = f"{module}.{alias.name}"
            self._imports[alias.asname or alias.name] = full
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._extract_symbol(node, "function")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._extract_symbol(node, "async_function")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        old_class = self._current_class
        class_name = f"{self._current_class}.{node.name}" if self._current_class else node.name
        self._current_class = class_name
        self._extract_symbol(node, "class")
        self.generic_visit(node)
        self._current_class = old_class

    def _extract_symbol(self, node: ast.AST, kind: str) -> None:
        if not hasattr(node, "name"):
            return

        qualified = f"{self._current_class}.{node.name}" if self._current_class else node.name
        if hasattr(node, "lineno"):
            line_start = node.lineno
            line_end = getattr(node, "end_lineno", node.lineno)
        else:
            line_start = line_end = 0

        # Extract signature
        signature = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [arg.arg for arg in node.args.args]
            signature = f"({', '.join(args)})"

        # Extract docstring
        docstring = ast.get_docstring(node)

        # Extract dependencies (calls, attribute accesses)
        deps = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    deps.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    deps.append(child.func.attr)
            elif isinstance(child, ast.Attribute):
                deps.append(child.attr)

        # Deduplicate
        deps = list(set(deps))

        content = self.content.split("\n")[line_start - 1:line_end]
        content_hash = hashlib.sha256("\n".join(content).encode("utf-8")).hexdigest()[:16]

        symbol = CodeSymbol(
            symbol_id=f"{self.file_path}:{qualified}",
            name=node.name,
            kind=kind,
            qualified_name=qualified,
            file_path=self.file_path,
            line_start=line_start,
            line_end=line_end,
            content_hash=content_hash,
            signature=signature,
            docstring=docstring,
            dependencies=deps,
        )
        self.symbols.append(symbol)


class DependencyGraph:
    """Code dependency graph for impact analysis."""

    def __init__(self) -> None:
        self.symbols: dict[str, CodeSymbol] = {}
        self.edges: dict[str, set[str]] = {}  # symbol_id -> set of dependent symbol_ids

    def add_symbols(self, symbols: list[CodeSymbol]) -> None:
        for sym in symbols:
            self.symbols[sym.symbol_id] = sym
            self.edges[sym.symbol_id] = set(sym.dependents)

    def get_affected_symbols(self, changed_symbol_ids: list[str], max_depth: int = 3) -> list[CodeSymbol]:
        """Find all symbols transitively affected by changes (up to max_depth)."""
        affected = set(changed_symbol_ids)
        frontier = set(changed_symbol_ids)

        for _ in range(max_depth):
            new_frontier = set()
            for sym_id in frontier:
                if sym_id in self.edges:
                    new_frontier.update(self.edges[sym_id])
            new_frontier -= affected
            if not new_frontier:
                break
            affected.update(new_frontier)
            frontier = new_frontier

        return [self.symbols[sid] for sid in affected if sid in self.symbols]


class CodeRAGIndexer:
    """Incremental Code RAG indexer using git diff + AST + dependency graph."""

    def __init__(
        self,
        repo_root: Path,
        version_controller: VersionController,
        chunk_manager: ChunkManager,
        qdrant_runtime: Any,  # QdrantCanonicalRuntime
        embedding_cache: Any,  # EmbeddingCache
    ) -> None:
        self.repo_root = repo_root
        self.version_controller = version_controller
        self.chunk_manager = chunk_manager
        self.qdrant = qdrant_runtime
        self.embedding_cache = embedding_cache
        self.git_extractor = GitDiffExtractor(repo_root)
        self.ast_analyzer = PythonASTAnalyzer(repo_root)
        self.dep_graph = DependencyGraph()

    async def index_commit(self, base_commit: str, head_commit: str, generation_id: str) -> CodeChangeSet:
        """Process a commit and incrementally update the index."""
        _logger.info("CodeRAGIndexer: processing commit %s..%s", base_commit[:8], head_commit[:8])

        # 1. Get file changes
        file_changes = self.git_extractor.get_changes_between(base_commit, head_commit)
        _logger.info("CodeRAGIndexer: %d files changed", len(file_changes))

        # 2. Analyze changed files
        python_files = [
            self.repo_root / fc.file_path
            for fc in file_changes
            if fc.file_path.endswith(".py") and fc.change_type != "deleted"
        ]

        all_symbols = self.ast_analyzer.get_symbols_for_files(python_files)
        self.dep_graph.add_symbols(all_symbols)

        # 3. Identify changed symbols (by content hash)
        changed_symbols = self._identify_changed_symbols(file_changes, all_symbols)

        # 4. Find transitive dependents
        changed_ids = [s.symbol_id for s in changed_symbols]
        affected = self.dep_graph.get_affected_symbols(changed_ids)

        # 5. Build change set
        change_set = CodeChangeSet(
            commit_hash=head_commit,
            commit_message=self._get_commit_message(head_commit),
            author=self._get_commit_author(head_commit),
            timestamp=datetime.now(timezone.utc).isoformat(),
            files=file_changes,
            symbols_added=[s for s in changed_symbols if self._is_new_symbol(s)],
            symbols_modified=[s for s in changed_symbols if not self._is_new_symbol(s)],
            symbols_deleted=self._get_deleted_symbols(file_changes),
            affected_symbols=affected,
        )

        # 6. Incremental Qdrant update
        await self._apply_incremental_update(change_set, generation_id)

        # 7. Invalidate AST cache for changed files
        for fc in file_changes:
            if fc.change_type in ("modified", "deleted"):
                self.ast_analyzer.invalidate_cache(self.repo_root / fc.file_path)

        return change_set

    def _identify_changed_symbols(
        self,
        file_changes: list[FileChange],
        all_symbols: list[CodeSymbol],
    ) -> list[CodeSymbol]:
        """Identify which symbols actually changed (content hash diff)."""
        changed = []
        for symbol in all_symbols:
            # Check if this symbol's file was modified
            for fc in file_changes:
                if symbol.file_path == fc.file_path or symbol.file_path == fc.new_path:
                    # In production: compare content_hash with stored version
                    changed.append(symbol)
                    break
        return changed

    def _is_new_symbol(self, symbol: CodeSymbol) -> bool:
        """Check if symbol is newly added (not in previous index)."""
        # Simplified: check if file was added
        return True  # Placeholder

    def _get_deleted_symbols(self, file_changes: list[FileChange]) -> list[CodeSymbol]:
        """Get symbols from deleted files."""
        deleted = []
        for fc in file_changes:
            if fc.change_type == "deleted" and fc.file_path.endswith(".py"):
                # In production: look up symbols from previous index
                pass
        return deleted

    def _get_commit_message(self, commit: str) -> str:
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%B", commit],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
                creationflags=CREATE_NO_WINDOW,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _get_commit_author(self, commit: str) -> str:
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%an <%ae>", commit],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
                creationflags=CREATE_NO_WINDOW,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    async def _apply_incremental_update(
        self,
        change_set: CodeChangeSet,
        generation_id: str,
    ) -> None:
        """Apply incremental updates to Qdrant via version controller."""
        # For each affected symbol, create chunk and upsert
        for symbol in change_set.affected_symbols:
            # Build chunk content (symbol + context)
            chunk_content = self._build_symbol_chunk(symbol)
            if not chunk_content:
                continue

            # Embedding (with cache)
            vector, from_cache = self.embedding_cache.get_or_compute(
                chunk_content,
                "qwen3-embedding:4b",
                2560,
                lambda c: self._embed(c),  # Placeholder
            )

            # Version-controlled upsert
            resource_id = f"symbol:{symbol.symbol_id}"
            module_id = "code-rag"

            current = self.version_controller.get_current_version(module_id, resource_id)
            expected_version = current.version if current else 0
            content_hash = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()

            success, new_version = self.version_controller.try_update_version(
                module_id=module_id,
                resource_id=resource_id,
                expected_version=expected_version,
                new_content_hash=content_hash,
                new_generation_id=generation_id,
                knowledge_kind=ResourceState.KnowledgeKind.SOURCE,
                new_state=ResourceState.CANONICAL_INDEXED,
                chunk_policy_version="code-v1",
                chunk_count=1,
                embedding_model="qwen3-embedding:4b",
                embedding_dimension=2560,
            )

            if success:
                # Upsert to Qdrant — payload contract: filterable metadata
                # only; content/file_path live in PostgreSQL, never Qdrant.
                from qdrant_client.http.models import PointStruct
                from .rag_qdrant import sanitize_payload
                point = PointStruct(
                    id=new_version.version,
                    vector=vector,
                    payload=sanitize_payload(
                        {
                            "resource_id": resource_id,
                            "module_id": module_id,
                            "content_hash": content_hash,
                            "generation_id": generation_id,
                            "symbol_id": symbol.symbol_id,
                            "symbol_kind": symbol.kind,
                            "line_start": symbol.line_start,
                            "line_end": symbol.line_end,
                        }
                    ),
                )
                if not await self.qdrant.upsert_points(
                    [point], generation_id=generation_id
                ):
                    _logger.error(
                        "CodeRAGIndexer: Qdrant upsert failed for %s:%s",
                        module_id,
                        resource_id,
                    )

    def _build_symbol_chunk(self, symbol: CodeSymbol) -> str:
        """Build searchable chunk content for a symbol."""
        parts = [f"{symbol.kind}: {symbol.qualified_name}{symbol.signature}"]
        if symbol.docstring:
            parts.append(f"Docstring: {symbol.docstring}")
        parts.append(f"File: {symbol.file_path}:{symbol.line_start}-{symbol.line_end}")
        if symbol.dependencies:
            parts.append(f"Dependencies: {', '.join(symbol.dependencies[:10])}")
        return "\n".join(parts)

    def _embed(self, content: str) -> list[float]:
        """Placeholder for actual embedding function."""
        # In production: call local embedding model
        import hashlib
        # Deterministic pseudo-embedding for testing
        seed = int(hashlib.sha256(content.encode()).hexdigest()[:8], 16)
        import random
        random.seed(seed)
        return [random.uniform(-1, 1) for _ in range(2560)]


__all__ = [
    "CodeSymbol",
    "FileChange",
    "CodeChangeSet",
    "GitDiffExtractor",
    "PythonASTAnalyzer",
    "DependencyGraph",
    "CodeRAGIndexer",
]