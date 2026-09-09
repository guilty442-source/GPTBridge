from __future__ import annotations

from typing import Any, Mapping


class StarNativeGroundingMixin:
    """Database evidence extraction and private/memory grounding helpers."""

    @staticmethod
    def _database_evidence(database: dict[str, Any]) -> list[dict[str, Any]]:
        tables = database.get("tables") if isinstance(database.get("tables"), dict) else {}
        quality = database.get("quality") if isinstance(database.get("quality"), dict) else {}
        return [
            {
                "id": "star-database:instrument-identities",
                "value": int(tables.get("instrument_identity") or 0),
            },
            {
                "id": "star-database:market-observations",
                "value": int(tables.get("market_observation") or 0),
            },
            {
                "id": "star-database:distribution-events",
                "value": int(tables.get("distribution_event") or 0),
            },
            {
                "id": "star-database:invalid-distribution-events",
                "value": int(quality.get("blank_distribution_events") or 0),
            },
        ]

    @staticmethod
    def _reviewed_memory_grounding(value: Any) -> dict[str, Any]:
        if not isinstance(value, list):
            return {"text": "", "evidence": []}
        lines: list[str] = []
        evidence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value[:12]:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("review_status") or "").strip().casefold() != "approved":
                continue
            content = " ".join(str(item.get("content") or "").split())[:1_200]
            memory_id = str(item.get("memory_id") or "").strip()[:96]
            if not content or not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            title = " ".join(str(item.get("title") or "").split())[:160]
            lines.append(f"- {title + '：' if title else ''}{content}")
            evidence.append(
                {
                    "id": f"star-memory:{memory_id}",
                    "memory_id": memory_id,
                    "source_type": str(item.get("source_type") or "local-memory"),
                    "source_id": str(item.get("source_id") or ""),
                    "confidence": float(item.get("confidence") or 0),
                    "review_status": "approved",
                    "retrieval_score": float(item.get("retrieval_score") or 0),
                }
            )
            if len(lines) >= 4:
                break
        return {"text": "\n".join(lines), "evidence": evidence}

    @staticmethod
    def _native_private_grounding(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"text": "", "counts": {}}
        training = value.get("training_examples")
        capabilities = value.get("capability_compositions")
        operations = value.get("operation_records")
        training = training if isinstance(training, list) else []
        capabilities = capabilities if isinstance(capabilities, list) else []
        operations = operations if isinstance(operations, list) else []
        lines: list[str] = []
        for item in training[:3]:
            if not isinstance(item, Mapping):
                continue
            target = " ".join(str(item.get("target_text") or "").split())[:600]
            if target:
                lines.append(f"- 已驗證訓練範例：{target}")
        for item in capabilities[:3]:
            if not isinstance(item, Mapping):
                continue
            capability_id = str(item.get("composition_id") or "").strip()[:160]
            status = str(item.get("status") or "").strip()[:64]
            if capability_id:
                lines.append(f"- 能力編成 {capability_id}：{status}")
        for item in operations[:3]:
            if not isinstance(item, Mapping):
                continue
            request = item.get("request")
            prompt = (
                " ".join(str(request.get("prompt") or "").split())[:400]
                if isinstance(request, Mapping)
                else ""
            )
            if prompt:
                lines.append(f"- 先前操作：{prompt}")
        return {
            "text": "\n".join(lines)[:4_000],
            "counts": {
                "training_examples": len(training),
                "capability_compositions": len(capabilities),
                "operation_records": len(operations),
            },
        }
