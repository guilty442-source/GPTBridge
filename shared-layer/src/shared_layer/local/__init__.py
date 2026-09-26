"""Local codex-native engines (本機引擎) for the shared layer.

Per Governance Codex A219/E21 (python+cpp hybrid, sole allowed stack) and
A37/E23 (local-owned code only, no third-party packages), every engine in
this package is local-owned; module-private structured state lives in
PostgreSQL schemas through :mod:`shared_layer.local.pg_adapter` (A610/A621
— PostgreSQL sole structured-data authority, SQLite/Qdrant retired).  An
optional C++ native kernel hook accelerates hot inner loops (A219
core-compute+native-perf).
"""

from __future__ import annotations

from .registry_repository import (
    LocalResourceRegistry,
    LocationRecord,
    ResourceRecord,
    ResourceRegistry,
)
from .module_locator import LocalModuleLocatorRepository, ModuleLocatorRepository
from .vector_store import LocalVectorStore
from .local_sqlite_rag_repository import LocalSqliteRagRepository

__all__: list[str] = [
    "LocalResourceRegistry",
    "ResourceRegistry",
    "LocationRecord",
    "ResourceRecord",
    "LocalModuleLocatorRepository",
    "ModuleLocatorRepository",
    "LocalVectorStore",
    "LocalSqliteRagRepository",
]