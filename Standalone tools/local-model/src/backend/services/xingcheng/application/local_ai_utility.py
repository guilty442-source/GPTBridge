from __future__ import annotations

import time
from typing import Any

from ..domain.model_registry import StarModelProfile, StarModelRegistry
from ..infrastructure.repository import LocalAiRepository


class LocalAiUtilityMixin:
    def _record_latency(self, operation: str, started: float) -> None:
        latency = round((time.perf_counter() - started) * 1000, 3)
        total_key = f"{operation}_latency_ms_total"
        last_key = f"{operation}_last_latency_ms"
        self._runtime_metrics[total_key] = round(
            float(self._runtime_metrics.get(total_key) or 0) + latency,
            3,
        )
        self._runtime_metrics[last_key] = latency

    @staticmethod
    def _identify_model(
        result: dict[str, Any], profile: StarModelProfile
    ) -> dict[str, Any]:
        result["model"] = profile.model_id
        result["model_name"] = profile.name
        result["model_role"] = profile.role
        result["model_network_policy"] = profile.network_policy
        result["model_transport"] = "governance-authenticated-ai-channel"
        result["coordinator_model"] = StarModelRegistry.MAIN.model_id
        result["coordination"] = "main-model-mediated"
        result["model_selection"] = "automatic"
        result["manual_model_selection"] = False
        result["delegated"] = profile != StarModelRegistry.MAIN
        return result

    def _repository_for(self, profile: StarModelProfile) -> LocalAiRepository:
        self.models.authorize_delegation(self.models.primary.model_id, profile)
        return self.repositories[profile.model_id]

    def _record_ollama_inference(
        self,
        result: dict[str, Any],
        *,
        intent: str,
        model_role: str,
        request: Any,
    ) -> None:
        model_id = str(result.get("model") or "")
        repository = getattr(self, "ollama_repositories", {}).get(model_id)
        if repository is None:
            return
        repository.record_inference(
            intent=intent,
            model_role=model_role,
            request=request,
            response=result,
        )
