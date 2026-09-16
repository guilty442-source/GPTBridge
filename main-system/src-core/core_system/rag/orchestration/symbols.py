"""Code RAG — Symbol Identity + Dependency Graph contracts.

Code chunks must not carry only ``chunk_id``/``resource_id``: three
files can all define ``query()``.  Every code chunk gets a Symbol
Identity so same-named symbols in different modules never collide:

    symbol_id = "python:shared_layer.rag_bridge.bridge:RagIndexCoordinator"

Qdrant answers "semantically similar to what"; PostgreSQL graph
relations answer "actually connected to what" — graph edges are
relational data, not vectors:

    gptbridge_rag.code_symbol (symbol_id, module_id, resource_id,
                               qualified_name, symbol_kind, language,
                               commit_sha)
    gptbridge_rag.code_edge   (source_symbol_id, target_symbol_id,
                               edge_type)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class SymbolKind(str, Enum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    CONSTANT = "constant"
    TYPE = "type"


class EdgeType(str, Enum):
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    IMPLEMENTS = "IMPLEMENTS"
    INHERITS = "INHERITS"
    REFERENCES = "REFERENCES"
    OWNS = "OWNS"


_SYMBOL_ID_RE = re.compile(r"^[a-z][a-z0-9_+\-]*:[A-Za-z0-9_.\-/]+:[A-Za-z0-9_.<>]+$")


@dataclass(frozen=True, slots=True)
class SymbolIdentity:
    """Qualified symbol identity bound to repo state."""

    language: str
    module_path: str            # e.g. "shared_layer.rag_bridge.bridge"
    qualified_name: str         # e.g. "RagIndexCoordinator.retrieve"
    symbol_kind: SymbolKind = SymbolKind.FUNCTION
    repository_id: str = ""
    branch: str = ""
    commit_sha: str = ""
    file_locator_id: str = ""

    @property
    def symbol_id(self) -> str:
        return f"{self.language}:{self.module_path}:{self.qualified_name}"


def make_symbol_id(language: str, module_path: str, qualified_name: str) -> str:
    """Canonical ``<lang>:<module>:<qualified>`` identifier."""
    lang = (language or "").strip().lower()
    mod = (module_path or "").strip()
    name = (qualified_name or "").strip()
    if not lang or not mod or not name:
        raise ValueError("symbol_id requires language, module_path, qualified_name")
    return f"{lang}:{mod}:{name}"


def valid_symbol_id(symbol_id: str) -> bool:
    return bool(_SYMBOL_ID_RE.match(symbol_id or ""))


@dataclass(frozen=True, slots=True)
class CodeSymbol:
    """``gptbridge_rag.code_symbol`` row contract."""

    symbol_id: str
    module_id: str
    resource_id: str
    qualified_name: str
    symbol_kind: SymbolKind
    language: str
    commit_sha: str = ""


@dataclass(frozen=True, slots=True)
class CodeEdge:
    """``gptbridge_rag.code_edge`` row contract — a real dependency
    relation between two symbols (never a vector)."""

    source_symbol_id: str
    target_symbol_id: str
    edge_type: EdgeType


@dataclass(frozen=True, slots=True)
class SymbolResolution:
    """Result of resolving a query mention to concrete symbols."""

    mention: str
    symbols: tuple[CodeSymbol, ...]
    ambiguous: bool = False      # several same-named symbols matched


__all__ = [
    "CodeEdge",
    "CodeSymbol",
    "EdgeType",
    "SymbolIdentity",
    "SymbolKind",
    "SymbolResolution",
    "make_symbol_id",
    "valid_symbol_id",
]
