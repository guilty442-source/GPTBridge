"""§10.6 RAG 增量索引一致性（定期 parity sweep）。

比對 PostgreSQL 權威（``index_state`` + ``chunk`` 實際列數）與 Qdrant
逐資源 point 數、embedding 版本、document hash；漂移資源逐一 enqueue
到 durable ``reconciliation_queue``，由既有 ``run_reconciliation`` 修復。

Fail-closed 規則：
- 只修復有問題的資源，不得整庫重建、不得刪除 Qdrant collection。
- 任何一側無法驗證（qdrant count 失敗／index_state 缺欄）視為
  ``unverifiable``，同樣 enqueue —— 中斷不得宣告成功。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .runtime_types import (
    QueueOperation,
    QueueStatus,
    ReconciliationQueueItem,
    make_idempotency_key,
)

_logger = logging.getLogger("gptbridge.rag.parity")


class RagParityAudit:
    """PG↔Qdrant 逐資源一致性掃描器。"""

    def __init__(self, postgresql: Any, qdrant: Any, config: Any) -> None:
        self.postgresql = postgresql
        self.qdrant = qdrant
        self.config = config

    async def sweep(self, *, module_id: Optional[str] = None) -> dict[str, Any]:
        """掃描所有非 tombstone 的 index_state 資源，回傳稽核報告。

        報告欄位：``checked``、``drifted``、``enqueued``、``unverifiable``、
        ``drifts``（逐資源漂移明細）、``duration_ms``。
        """
        started = time.monotonic()
        report: dict[str, Any] = {
            "checked": 0,
            "drifted": 0,
            "enqueued": 0,
            "unverifiable": 0,
            "drifts": [],
            "duration_ms": 0.0,
        }
        rows = await self.postgresql.list_index_state_details(module_id)
        pg_counts = await self.postgresql.chunk_count_by_resource(module_id)
        if rows is None or pg_counts is None:
            report["error"] = "postgresql-authority-unavailable"
            report["duration_ms"] = (time.monotonic() - started) * 1000.0
            return report

        for row in rows:
            report["checked"] += 1
            rid = str(row["resource_id"])
            mid = str(row["module_id"])
            reasons: list[str] = []

            pg_chunks = int(pg_counts.get((mid, rid), 0))
            declared = int(row.get("chunk_count") or 0)
            if declared != pg_chunks:
                reasons.append("pg-chunk-count-mismatch")

            qdrant_points = self.qdrant.count_resource_points(mid, rid)
            if qdrant_points is None:
                reasons.append("qdrant-unverifiable")
                report["unverifiable"] += 1
            elif qdrant_points != pg_chunks:
                reasons.append("point-count-mismatch")

            if not row.get("content_hash"):
                reasons.append("missing-content-hash")
            if (
                str(row.get("embedding_model") or "") != str(self.config.embedding_model)
                or int(row.get("embedding_version") or 0)
                != int(getattr(self.config, "embedding_version", 1) or 1)
            ):
                reasons.append("embedding-version-drift")
            if str(row.get("status") or "") != "indexed":
                reasons.append("status-not-indexed")

            if not reasons:
                continue
            report["drifted"] += 1
            report["drifts"].append(
                {
                    "module_id": mid,
                    "resource_id": rid,
                    "reasons": reasons,
                    "declared_chunks": declared,
                    "pg_chunks": pg_chunks,
                    "qdrant_points": qdrant_points,
                }
            )
            if await self._enqueue(mid, rid, row, reasons):
                report["enqueued"] += 1

        report["duration_ms"] = (time.monotonic() - started) * 1000.0
        _logger.info(
            "RagParityAudit: checked=%d drifted=%d enqueued=%d unverifiable=%d",
            report["checked"],
            report["drifted"],
            report["enqueued"],
            report["unverifiable"],
        )
        return report

    async def _enqueue(
        self, module_id: str, resource_id: str, row: dict[str, Any], reasons: list[str]
    ) -> bool:
        """把漂移資源送入 durable reconciliation_queue（idempotent）。"""
        content_hash = str(row.get("content_hash") or "")
        source_revision = int(row.get("source_revision") or 1)
        operation = QueueOperation.UPDATE.value
        now = datetime.now(timezone.utc).isoformat()
        item = ReconciliationQueueItem(
            operation_id=f"parity-{uuid.uuid4().hex[:16]}",
            idempotency_key=make_idempotency_key(
                module_id=module_id,
                resource_id=resource_id,
                operation=operation,
                source_revision=source_revision,
                content_hash=content_hash,
            ),
            resource_id=resource_id,
            locator_id=f"{module_id}:{resource_id}",
            source_revision=source_revision,
            content_hash=content_hash,
            operation=operation,
            tombstone_generation=0,
            embedding_model=str(self.config.embedding_model),
            embedding_version=int(getattr(self.config, "embedding_version", 1) or 1),
            chunk_size=int(getattr(self.config, "chunk_size", 0) or 0),
            chunk_overlap=int(getattr(self.config, "chunk_overlap", 0) or 0),
            chunking_version=int(row.get("chunking_version") or 1),
            parser_version=int(row.get("parser_version") or 1),
            schema_version=int(row.get("rag_schema_version") or 1),
            created_at=now,
            status=QueueStatus.PENDING.value,
            payload={"module_id": module_id, "parity_reasons": reasons},
        )
        try:
            return bool(await self.postgresql.enqueue_reconciliation(item))
        except Exception as exc:  # pragma: no cover - defensive
            _logger.error(
                "RagParityAudit: enqueue failed for %s:%s: %s", module_id, resource_id, exc
            )
            return False
