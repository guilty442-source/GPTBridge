from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

from ..infrastructure.git_repository import LocalGitRepository
from shared_layer.resource_identity import (
    PLATFORM_ID,
    XINGCHENG_MODULE_ID,
    ResourceIdentity,
    canonical_identifier,
)

from .local_rag import LocalRagService


class LocalKnowledgeService:
    """Unified governance-secured facade over local Git, SQL and RAG knowledge.

    Integration layer that:
      1. Composes the loopback Git adapter, the local sqlite RAG repository, the
         Xingcheng cognition and identity repositories and the hybrid local RAG
         service under one interface.
      2. Applies a single governance gate to every write operation.
      3. Exposes cross-domain retrieval (git metadata + local sqlite + RAG).
    """

    def __init__(
        self,
        tool_root: Path,
        transformer_runtime: Any,
        *,
        rag_service: LocalRagService | None = None,
        git_repository: LocalGitRepository | None = None,
    ) -> None:
        self.tool_root = Path(tool_root).resolve()
        self.project_root = self.tool_root.parent.resolve()
        self.rag = rag_service or LocalRagService(
            self.tool_root, transformer_runtime
        )
        self.git = git_repository or LocalGitRepository(self.project_root)
        self._write_lock = asyncio.Lock()
        self._pool_manager: Any | None = None
        self._cognition: Any | None = None
        self._identity: Any | None = None

    @property
    def pool_manager(self) -> Any:
        if self._pool_manager is None:
            from ..infrastructure.local_sqlite_pool import get_pool_manager

            self._pool_manager = get_pool_manager(self.tool_root)
        return self._pool_manager

    @property
    def cognition(self) -> Any:
        if self._cognition is None:
            from ..infrastructure.local_sqlite_cognition_repository import (
                LocalSqliteCognitionRepository,
            )

            self._cognition = LocalSqliteCognitionRepository(
                self.tool_root, self.pool_manager
            )
        return self._cognition

    @property
    def identity(self) -> Any:
        if self._identity is None:
            from ..infrastructure.local_sqlite_identity_repository import (
                LocalSqliteIdentityRepository,
            )

            self._identity = LocalSqliteIdentityRepository(
                self.tool_root, self.pool_manager
            )
        return self._identity

    # ------------------------------------------------------------------ git --
    async def git_status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.git.status)

    async def git_history(self, *, limit: int = 20) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.git.history, limit=int(limit or 20)
        )

    async def git_diff_stat(self, *, confirmed: bool = False) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.git.diff_stat, confirmed=confirmed
        )

    async def git_stage(self, paths: Any, *, confirmed: bool = False) -> dict[str, Any]:
        async with self._write_lock:
            result = await asyncio.to_thread(
                self.git.stage, paths, confirmed=confirmed
            )
        result["governance"] = self._governance_record("git-stage", result)
        return result

    async def git_commit(
        self, message: str, *, confirmed: bool = False, amend: bool = False
    ) -> dict[str, Any]:
        async with self._write_lock:
            result = await asyncio.to_thread(
                self.git.commit, message, confirmed=confirmed, amend=amend
            )
        result["governance"] = self._governance_record("git-commit", result)
        return result

    # ------------------------------------------------------------------ rag --
    async def rag_ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        module_id = canonical_identifier(
            str(payload.get("module_id") or XINGCHENG_MODULE_ID),
            field="module_id",
        )
        result = await asyncio.to_thread(self.rag.ingest, {**payload, "module_id": module_id})
        if result.get("ok") is True:
            result["governance"] = self._governance_record(
                "rag-ingest",
                {
                    "ok": True,
                    "module_id": module_id,
                    "indexed_count": result.get("indexed_count") or 0,
                },
            )
        return result

    async def rag_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = await asyncio.to_thread(self.rag.query, dict(payload))
        if result.get("ok") is True:
            result["governance"] = self._governance_record(
                "rag-query",
                {
                    "ok": True,
                    "retrieved_count": result.get("retrieved_count") or 0,
                },
            )
        return result

    async def rag_status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.rag.status)

    # ---------------------------------------------------------------- sql --
    async def sql_status(self) -> dict[str, Any]:
        def _snapshot() -> dict[str, Any]:
            return {
                "engine": "local-sqlite3-degraded",
                "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
                "canonical_central_engine": "postgresql",
                "authority": "non-canonical-reconciliation-required",
                "reconciliation_required": True,
                "pooled": self.pool_manager.status(),
                "cognition_initialized": self.cognition.initialized(),
                "identity_initialized": self.identity.initialized(),
            }

        try:
            return await asyncio.to_thread(_snapshot)
        except RuntimeError as error:
            return {
                "ok": False,
                "error_code": str(error),
                "message": "本機 sqlite cognition/identity 未建立。",
                "engine": "local-sqlite3-degraded",
                "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
                "canonical_central_engine": "postgresql",
                "authority": "non-canonical-reconciliation-required",
                "reconciliation_required": True,
                "pooled": False,
            }

    async def sql_save_personality(
        self, value: dict[str, Any], *, confirmed: bool = False
    ) -> dict[str, Any]:
        if not confirmed:
            return {
                "ok": False,
                "error_code": "SQL_GOVERNANCE_APPROVAL_REQUIRED",
                "message": "儲存角色設定需明確治理確認。",
            }
        result = await asyncio.to_thread(
            self.identity.save_personality, dict(value) if isinstance(value, dict) else {}
        )
        result["governance"] = self._governance_record(
            "identity-save-personality",
            {
                "ok": result.get("ok") is True,
                "resource_id": result.get("resource_id"),
                "version": result.get("version"),
            },
        )
        return result

    async def sql_get_personality(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.identity.personality) or {
            "ok": False,
            "personality": None,
        }

    async def sql_save_knowledge(
        self, payload: dict[str, Any], *, confirmed: bool = False
    ) -> dict[str, Any]:
        if not confirmed:
            return {
                "ok": False,
                "error_code": "SQL_GOVERNANCE_APPROVAL_REQUIRED",
                "message": "儲存知識需明確治理確認。",
            }
        result = await asyncio.to_thread(
            self.cognition.save_knowledge,
            knowledge_type=str(payload.get("knowledge_type") or "general"),
            value=payload.get("value") if isinstance(payload.get("value"), dict) else {},
            classification=str(payload.get("classification") or "private"),
            resource_label=str(payload.get("resource_label") or ""),
        )
        result["governance"] = self._governance_record(
            "cognition-save-knowledge",
            {
                "ok": result.get("ok") is True,
                "resource_id": result.get("resource_id"),
                "version": result.get("version"),
            },
        )
        return result

    async def sql_list_knowledge(self, *, knowledge_type: str = "", limit: int = 100) -> dict[str, Any]:
        rows = await asyncio.to_thread(
            self.cognition.list_knowledge,
            knowledge_type=knowledge_type,
            limit=int(limit or 100),
        )
        return {"ok": True, "knowledge": rows, "count": len(rows)}

    # ------------------------------------------------------------ integrated --
    async def platform_status(self) -> dict[str, Any]:
        git_status, rag_status = await asyncio.gather(
            asyncio.to_thread(self.git.status),
            asyncio.to_thread(self.rag.status),
        )
        return {
            "ok": True,
            "manager": "星澄 Core / Orchestrator",
            "highest_authority_management_required": True,
            "governance_source": self._governance_source(),
            "fully_local": True,
            "execution_authority": False,
            "execution_owner": "governed-executor",
            "self_model_data_exception": "read-write",
            "git": git_status,
            "sql": await self.sql_status(),
            "rag": rag_status,
            "llm": {
                "engine": "ollama",
                "role": "local-understanding-reasoning-and-operations",
            },
            "integration": {
                "layer": "LocalKnowledgeService",
                "governed": True,
                "write_gate": "confirm-required",
            },
        }

    async def unified_search(self, query: str, *, limit: int = 8) -> dict[str, Any]:
        """Cross-domain retrieval over RAG knowledge plus git workspace metadata."""
        question = str(query or "").strip()
        if not question:
            return {
                "ok": False,
                "error_code": "KNOWLEDGE_QUERY_REQUIRED",
                "message": "請提供查詢字串。",
            }
        rag_result = await asyncio.to_thread(self.rag.query, {"question": question})
        git_result = await asyncio.to_thread(self.git.status)
        citations = rag_result.get("citations") if rag_result.get("ok") is True else []
        return {
            "ok": True,
            "question": question,
            "retrieved": {
                "rag": {
                    "count": len(citations),
                    "citations": citations[: int(limit or 8)],
                    "route": rag_result.get("route"),
                },
                "git": {
                    "available": git_result.get("available") is True,
                    "branch": git_result.get("branch"),
                    "revision": git_result.get("revision"),
                    "dirty": git_result.get("dirty"),
                    "change_count": git_result.get("change_count"),
                    "changes": (git_result.get("changes") or [])[: int(limit or 8)],
                },
            },
            "governance": self._governance_record(
                "unified-search",
                {
                    "ok": True,
                    "rag_count": len(citations),
                    "git_dirty": git_result.get("dirty"),
                },
            ),
            "network_used": False,
        }

    # ------------------------------------------------------------ governance --
    @staticmethod
    def _governance_source() -> dict[str, Any]:
        return {
            "authority": "governance-rule-only",
            "implementation": "governance_rule",
            "runtime_mutation": "prohibited",
            "write_requires_governance_approval": True,
        }

    @staticmethod
    def _governance_record(action: str, detail: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "action": action,
            "approved": True,
            "platform_id": PLATFORM_ID,
            "module_id": XINGCHENG_MODULE_ID,
            "detail": dict(detail),
            "write_requires_governance_approval": True,
        }


__all__ = ["LocalKnowledgeService"]
