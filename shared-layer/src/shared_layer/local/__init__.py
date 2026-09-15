"""Local codex-native engines (本機引擎) for the shared layer.

Per Governance Codex A219/E21 (python+cpp hybrid, sole allowed stack) and A37/E23
(local-owned code only, no third-party packages or external services), every
engine in this package is implemented with the Python standard library only
(sqlite3, pathlib, hashlib, math), with an optional C++ native kernel hook for
hot inner loops (A219 core-compute+native-perf).  There is deliberately NO
dependency on PostgreSQL/psycopg, Qdrant, numpy, scipy, or any other external
package/service.
"""

from __future__ import annotations

from .sqlite_store import LocalSharedLayerStore, SharedLayerStore
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
    "LocalSharedLayerStore",
    "SharedLayerStore",
    "LocalResourceRegistry",
    "ResourceRegistry",
    "LocationRecord",
    "ResourceRecord",
    "LocalModuleLocatorRepository",
    "ModuleLocatorRepository",
    "LocalVectorStore",
    "LocalSqliteRagRepository",
]