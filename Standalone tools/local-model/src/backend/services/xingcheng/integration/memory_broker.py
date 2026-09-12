from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.model_registry import StarModelProfile, StarModelRegistry
from ..infrastructure.repository import LocalAiRepository


class StarMemoryBroker:
    """Copies reviewed memory between isolated model databases through Star."""

    def __init__(
        self,
        repositories: dict[str, LocalAiRepository],
        registry: StarModelRegistry,
    ) -> None:
        self.repositories = repositories
        self.registry = registry
        self.main = repositories[registry.MAIN.model_id]

    def context_for_external(
        self, business_scope: str, task_type: str, *, limit: int = 12
    ) -> list[dict[str, Any]]:
        profiles = [self.registry.MAIN]
        if business_scope == "investment":
            profiles.append(self.registry.INVESTMENT)
        if task_type in {"calculation", "reasoning"}:
            profiles.append(self.registry.MATHEMATICAL)
        if task_type in {"coding", "self_upgrade"}:
            profiles.append(self.registry.CODING)
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for profile in profiles:
            for item in self.repositories[profile.model_id].memory_context(
                business_scope, limit=limit
            ):
                digest = str(item.get("content") or "")
                if not digest or digest in seen:
                    continue
                seen.add(digest)
                output.append(item)
                if len(output) >= limit:
                    return output
        return output

    @staticmethod
    def _retrieval_terms(value: Any) -> set[str]:
        normalized = str(value or "").casefold()
        latin = re.findall(r"[a-z][a-z0-9_-]{1,}", normalized)
        chinese = [
            run[index : index + 2]
            for run in re.findall(r"[\u3400-\u9fff]{2,}", normalized)
            for index in range(len(run) - 1)
        ]
        return set(latin + chinese)

    def context_for_inference(
        self,
        business_scope: str,
        task_type: str,
        query: str,
        *,
        limit: int = 6,
        owner_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return only approved, query-relevant memory for native inference.

        Internal task logs are continuity hints rather than factual knowledge, so
        they are retrieved only when the user explicitly refers to earlier work.
        """

        bounded_limit = max(1, min(12, int(limit)))
        normalized_query = str(query or "").strip()
        query_terms = self._retrieval_terms(normalized_query)
        continuity_requested = bool(
            re.search(
                r"上次|之前|先前|剛才|繼續|接續|延續|previous|earlier|last\s+time|continue",
                normalized_query,
                flags=re.IGNORECASE,
            )
        )
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        candidates = (
            self.main.memory_context(business_scope, limit=max(24, bounded_limit * 4))
            if owner_only
            else self.context_for_external(
                business_scope, task_type, limit=max(24, bounded_limit * 4)
            )
        )
        for item in candidates:
            source_type = str(item.get("source_type") or "").casefold()
            if source_type == "star-internal" and not continuity_requested:
                continue
            memory_terms = self._retrieval_terms(
                f"{item.get('title') or ''}\n{item.get('content') or ''}"
            )
            overlap = len(query_terms & memory_terms)
            similarity = (
                2 * overlap / (len(query_terms) + len(memory_terms))
                if query_terms and memory_terms
                else 0.0
            )
            if overlap == 0 and not (
                continuity_requested and source_type == "star-internal"
            ):
                continue
            ranked_item = dict(item)
            ranked_item["retrieval_score"] = round(similarity, 4)
            ranked.append(
                (
                    similarity + 0.05 * float(item.get("confidence") or 0),
                    str(item.get("updated_at") or ""),
                    ranked_item,
                )
            )
        ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [item for _, _, item in ranked[:bounded_limit]]

    def accept_external_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        business_scope: str,
        task_type: str,
    ) -> list[dict[str, Any]]:
        target = self._target_profile(business_scope, task_type)
        targets = [self.registry.MAIN]
        if target != self.registry.MAIN:
            targets.append(target)
        accepted: list[dict[str, Any]] = []
        for candidate in candidates[:10]:
            if not isinstance(candidate, dict):
                continue
            content = str(candidate.get("content") or "").strip()
            if not content or str(candidate.get("status") or "candidate") != "candidate":
                continue
            memory_id = uuid.uuid4().hex[:24]
            for profile in targets:
                stored = self.repositories[profile.model_id].store_brokered_memory(
                    memory_id=memory_id,
                    kind=str(candidate.get("kind") or task_type),
                    title=str(candidate.get("title") or content[:80]),
                    content=content,
                    business_scope=business_scope,
                    source_type="external-ai-candidate",
                    source_id=str(candidate.get("candidate_id") or "external-ai"),
                    source_model_id=str(candidate.get("source_agent_id") or "external-ai"),
                    broker_model_id=self.registry.MAIN.model_id,
                    confidence=0.55,
                    expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                    review_status="pending-review",
                    provenance={
                        "candidate_id": str(candidate.get("candidate_id") or "external-ai"),
                        "source_agent_id": str(candidate.get("source_agent_id") or "external-ai"),
                        "received_via": "governance-authenticated-ai-channel",
                        "direct_external_write": False,
                    },
                )
                accepted.append(stored)
        return accepted

    def remember_internal_task(
        self,
        profile: StarModelProfile,
        *,
        business_scope: str,
        prompt: str,
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        normalized_prompt = " ".join(str(prompt or "").split())[:800]
        if not normalized_prompt:
            return []
        summary = json.dumps(
            {
                "instruction": normalized_prompt,
                "intent": str(result.get("intent") or "general"),
                "status": (
                    result.get("instruction_execution", {}).get("status")
                    if isinstance(result.get("instruction_execution"), dict)
                    else "completed"
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        profiles = [self.registry.MAIN]
        if profile != self.registry.MAIN:
            profiles.append(profile)
        stored: list[dict[str, Any]] = []
        memory_id = uuid.uuid4().hex[:24]
        for owner in profiles:
            stored.append(
                self.repositories[owner.model_id].store_brokered_memory(
                    memory_id=memory_id,
                    kind="instruction",
                    title=normalized_prompt[:120],
                    content=summary,
                    business_scope=business_scope,
                    source_type="star-internal",
                    source_id=profile.model_id,
                    source_model_id=profile.model_id,
                    broker_model_id=self.registry.MAIN.model_id,
                    confidence=0.8,
                    expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
                    review_status="approved",
                    provenance={
                        "origin": "star-internal-task",
                        "coordinator": self.registry.MAIN.model_id,
                    },
                )
            )
        return stored

    def status(self) -> dict[str, Any]:
        records = {
            profile.model_id: self.repositories[profile.model_id].memory_records(
                include_inactive=True,
                limit=500,
            )
            for profile in self.registry.profiles
        }
        return {
            "mode": "star-mediated-copy",
            "broker_model": self.registry.MAIN.model_id,
            "direct_peer_database_access": False,
            "external_direct_write": False,
            "review_required_for_external": True,
            "expiry_enforced": True,
            "provenance_required": True,
            "models": {
                model_id: {
                    "total": len(items),
                    "approved": sum(item["review_status"] == "approved" for item in items),
                    "pending_review": sum(item["review_status"] == "pending-review" for item in items),
                    "rejected": sum(item["review_status"] == "rejected" for item in items),
                    "revoked": sum(item["review_status"] == "revoked" for item in items),
                }
                for model_id, items in records.items()
            },
        }

    def list_memories(
        self,
        *,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for profile in self.registry.profiles:
            output.extend(
                self.repositories[profile.model_id].memory_records(
                    include_inactive=include_inactive,
                    limit=limit,
                )
            )
        grouped: dict[str, dict[str, Any]] = {}
        for item in output:
            memory_id = str(item.get("memory_id") or "")
            existing = grouped.get(memory_id)
            if existing is None:
                existing = dict(item)
                existing["owner_model_ids"] = [str(item.get("owner_model_id") or "")]
                grouped[memory_id] = existing
            else:
                existing["owner_model_ids"].append(
                    str(item.get("owner_model_id") or "")
                )
        records = list(grouped.values())
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return records[: max(1, min(500, int(limit)))]

    def review_memory(
        self,
        memory_id: str,
        *,
        action: str,
        reviewer: str,
        reason: str = "",
    ) -> list[dict[str, Any]]:
        reviewed: list[dict[str, Any]] = []
        for repository in self.repositories.values():
            try:
                reviewed.append(
                    repository.review_memory(
                        memory_id,
                        action=action,
                        reviewer=reviewer,
                        reason=reason,
                    )
                )
            except KeyError:
                continue
        if not reviewed:
            raise KeyError("memory not found")
        return reviewed

    def _target_profile(
        self, business_scope: str, task_type: str
    ) -> StarModelProfile:
        if task_type in {"calculation", "reasoning"}:
            return self.registry.MATHEMATICAL
        if task_type in {"coding", "self_upgrade"}:
            return self.registry.CODING
        if business_scope == "investment":
            return self.registry.INVESTMENT
        return self.registry.MAIN
