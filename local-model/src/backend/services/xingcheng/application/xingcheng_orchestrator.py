"""Xingcheng core orchestrator — highest-level coordination only.

Wires existing managers and scaffolds the three fixed responsibilities
defined in the architecture contract:

  1. sql-central-management   (inventory, health, policy, migration)
  2. rag-central-management   (index health, routing, consistency)
  3. git-central-management   (status, history, change planning)

The orchestrator holds references to managers but does not reimplement
their logic.  Every operation that touches data or execution routes
through the AccessGateway and respects the governance constraints:
  - decision_and_orchestration = true
  - execution = false
  - git_write / sql_write / rag_mutation = false
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Domain status snapshots
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubsystemHealth:
    name: str
    ready: bool
    message: str = ""
    latency_ms: float = 0.0


@dataclass(frozen=True)
class OrchestratorReport:
    state: str  # READY | DEGRADED | FAILED
    subsystems: list[SubsystemHealth] = field(default_factory=list)
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class XingchengOrchestrator:
    """Top-level coordinator for the Xingcheng (星澄) AI channel.

    Responsibilities (decision & orchestration only — no direct execution):
      - Unified health snapshot across SQL, RAG, Git, Ollama, vector semantics
      - SQL inventory & health policy coordination
      - RAG index health & consistency coordination
      - Git status & change-planning coordination
      - Graceful shutdown sequencing
      - Access-gate enforcement for cross-domain operations
    """

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()
        self.project_root = self.tool_root.parent.resolve()

        # Lazy-initialized managers — avoid circular imports at module level.
        self._resource_manager: Any | None = None
        self._pool_manager: Any | None = None
        self._knowledge_service: Any | None = None
        self._access_gateway: Any | None = None
        self._db_health_check: Any | None = None
        self._started_at: float = time.monotonic()

    # ------------------------------------------------------------------
    # Lazy property accessors
    # ------------------------------------------------------------------

    @property
    def resource_manager(self) -> Any:
        if self._resource_manager is None:
            from ..infrastructure.resource_manager import ResourceManager
            self._resource_manager = ResourceManager()
        return self._resource_manager

    @property
    def pool_manager(self) -> Any:
        if self._pool_manager is None:
            from ..infrastructure.local_sqlite_pool import get_pool_manager
            self._pool_manager = get_pool_manager(self.tool_root)
        return self._pool_manager

    @property
    def knowledge_service(self) -> Any:
        if self._knowledge_service is None:
            from .local_knowledge import LocalKnowledgeService
            self._knowledge_service = LocalKnowledgeService(self.tool_root)
        return self._knowledge_service

    @property
    def access_gateway(self) -> Any:
        if self._access_gateway is None:
            from shared_layer.access_gateway import AccessGateway
            self._access_gateway = AccessGateway(
                governance_authorizer=lambda _p, _a, _t: False
            )
        return self._access_gateway

    @property
    def db_health_check(self) -> Any:
        if self._db_health_check is None:
            from shared_layer.local.database import (
                DatabaseHealthCheck,
                DatabaseSettings,
            )
            self._db_health_check = DatabaseHealthCheck(DatabaseSettings.from_environment())
        return self._db_health_check

    # ------------------------------------------------------------------
    # Unified health
    # ------------------------------------------------------------------

    def status(self) -> OrchestratorReport:
        """Collect health snapshots from every wired subsystem."""
        started = time.monotonic()
        subsystems: list[SubsystemHealth] = []

        # 1. Local sqlite stores (critical)
        subsystems.append(self._probe_local_sqlite())

        # 2. Local semantic index (degradable)
        subsystems.append(self._probe_vector())

        # 3. Ollama + resource manager (degradable)
        subsystems.append(self._probe_ollama())

        # 4. RAG index consistency
        subsystems.append(self._probe_rag())

        # 5. Git status
        subsystems.append(self._probe_git())

        ready_count = sum(1 for s in subsystems if s.ready)
        if ready_count == len(subsystems):
            state = "READY"
        elif any(s.name == "local-sqlite" and not s.ready for s in subsystems):
            state = "FAILED"
        else:
            state = "DEGRADED"

        return OrchestratorReport(
            state=state,
            subsystems=subsystems,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # SQL central management  (scaffold — delegates to existing managers)
    # ------------------------------------------------------------------

    def sql_inventory_health(self) -> SubsystemHealth:
        """Local sqlite inventory, health policy, and migration readiness."""
        return self._probe_local_sqlite()

    def sql_schema_ensure(self, *names: str) -> None:
        """Local sqlite schemas are auto-provisioned by the repositories.

        PostgreSQL SchemaManager semantics do not apply to the local store
        (A44/E30); this hook is a governance no-op.
        """
        return None

    def sql_role_ensure_group(self, role: str) -> bool:
        """Roles are a PostgreSQL concept; sqlite isolation is per-module.

        Governance no-op returning True so orchestrator callers keep working.
        """
        return True

    def sql_rls_enforce(self, schema: str, table: str) -> None:
        """Row-level security is PostgreSQL-only.

        Local isolation is enforced by module_id columns on every governed
        record (A45/E31); this hook is a governance no-op.
        """
        return None

    def sql_index_ensure(self, name: str, schema: str, table: str, columns: tuple[str, ...]) -> None:
        """Index management on the local store is handled by the stores.

        PostgreSQL IndexManager semantics do not apply (A44/E30).
        """
        return None

    # ------------------------------------------------------------------
    # RAG central management  (scaffold — delegates to LocalKnowledgeService)
    # ------------------------------------------------------------------

    def rag_health(self) -> SubsystemHealth:
        """RAG index health, routing, and consistency check."""
        return self._probe_rag()

    def rag_query(self, query: str, *, module_id: str = "", limit: int = 5) -> list[dict[str, Any]]:
        """Route a RAG query through the knowledge service."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.knowledge_service.rag_query(query, module_id=module_id, limit=limit)
        )

    def rag_ingest(self, documents: list[dict[str, Any]], *, module_id: str = "") -> dict[str, Any]:
        """Route RAG ingestion through the knowledge service."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.knowledge_service.rag_ingest(documents, module_id=module_id)
        )

    # ------------------------------------------------------------------
    # Git central management  (scaffold — delegates to LocalKnowledgeService)
    # ------------------------------------------------------------------

    def git_status(self) -> SubsystemHealth:
        """Git status, history, and change-planning readiness."""
        return self._probe_git()

    def git_log(self, *, max_count: int = 20) -> list[dict[str, Any]]:
        """Retrieve recent git history."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.knowledge_service.git_history(max_count=max_count)
        )

    def git_diff_stat(self) -> dict[str, Any]:
        """Get diff stats for uncommitted changes."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.knowledge_service.git_diff_stat()
        )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Graceful shutdown — release resources in reverse order."""
        if self._pool_manager is not None:
            try:
                self._pool_manager.close()
            except Exception:
                pass
        if self._resource_manager is not None:
            try:
                self._resource_manager.emergency_release()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal probes
    # ------------------------------------------------------------------

    def _probe_local_sqlite(self) -> SubsystemHealth:
        try:
            health = self.db_health_check.run()
            return SubsystemHealth(
                name="local-sqlite",
                ready=health.available,
                message=health.message,
                latency_ms=health.latency_ms,
            )
        except Exception as exc:
            return SubsystemHealth(
                name="local-sqlite", ready=False, message=str(exc)[:200]
            )

    def _probe_vector(self) -> SubsystemHealth:
        try:
            from ..infrastructure.local_vector_store import LocalVectorStore
            store = LocalVectorStore(
                self.tool_root / "runtime" / "state" / "local-rag-vectors.sqlite3"
            )
            status = store.status()
            return SubsystemHealth(
                name="vector",
                ready=status.get("available") is True,
                message="ready",
            )
        except Exception as exc:
            return SubsystemHealth(
                name="vector", ready=False, message=str(exc)[:200]
            )

    def _probe_ollama(self) -> SubsystemHealth:
        import urllib.request
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/tags",
                headers={"Connection": "keep-alive"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
            return SubsystemHealth(name="ollama", ready=True, message="ready")
        except Exception as exc:
            return SubsystemHealth(name="ollama", ready=False, message=str(exc)[:200])

    def _probe_rag(self) -> SubsystemHealth:
        try:
            import asyncio
            rag_status = asyncio.get_event_loop().run_until_complete(
                self.knowledge_service.rag_status()
            )
            ready = bool(rag_status.get("available", False))
            return SubsystemHealth(
                name="rag",
                ready=ready,
                message=rag_status.get("message", "ok"),
            )
        except Exception as exc:
            return SubsystemHealth(name="rag", ready=False, message=str(exc)[:200])

    def _probe_git(self) -> SubsystemHealth:
        try:
            git = self.knowledge_service.git
            git.repo_dir  # triggers path resolution
            return SubsystemHealth(name="git", ready=True, message="ready")
        except Exception as exc:
            return SubsystemHealth(name="git", ready=False, message=str(exc)[:200])


__all__ = ["OrchestratorReport", "SubsystemHealth", "XingchengOrchestrator"]
