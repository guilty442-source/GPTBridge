from __future__ import annotations

from typing import Any

from .investment_analysis import ANALYSIS_MODEL_KEYS


class LocalAiHealthMixin:
    @staticmethod
    def _governance_source_status() -> dict[str, Any]:
        """Read sealed governance snapshots without acquiring mutation authority."""
        from governance_rule.governance_policy import governance_policy_snapshot
        from governance_rule.permission_directory.directory_authority import (
            directory_authority_snapshot,
        )

        policy = governance_policy_snapshot()
        directory = directory_authority_snapshot()
        return {
            "connected": True,
            "mode": "direct-read-only",
            "authoritative": True,
            "source_priority": "highest",
            "governance_authority_version": policy.authority_version,
            "governance_authority": policy.authority,
            "directory_managing_authority": directory.managing_authority,
            "write_allowed": False,
            "execute_allowed": False,
            "bypass_allowed": False,
        }

    def runtime_health(self) -> dict[str, Any]:
        transformer_status = self.transformer_runtime.status()
        llm_ready = transformer_status.get("available") is True
        rag_status = self.local_rag.status()
        return {
            "service_ready": True,
            "platform_mode": self.PLATFORM_MODE,
            "entry_gateway": self.ENTRY_GATEWAY,
            "highest_authority_management_required": True,
            "central_management_responsibilities": list(
                self.CENTRAL_MANAGEMENT_RESPONSIBILITIES
            ),
            "governance_source": self._governance_source_status(),
            "platform_permission_profile": self.PLATFORM_PERMISSION_PROFILE,
            "platform_labels": [dict(item) for item in self.PLATFORM_LABELS],
            "star_native_model_permissions": dict(
                self.STAR_NATIVE_MODEL_PERMISSIONS
            ),
            "platform_concurrency": "asynchronous-service-isolated",
            "platform_service_count": len(self.PLATFORM_SERVICES),
            "platform_services": dict(self.PLATFORM_SERVICES),
            "main_system_companion_tools": ["star-chat"],
            "model_dialogue": {
                "tool_id": "star-chat",
                "independent_only_in": "main-system",
                "physical_owner_root": "local-model",
                "settings_owner": "xingcheng",
                "business_layer_owner": "xingcheng",
                "permission_profile": self.PLATFORM_PERMISSION_PROFILE,
                "cache_owner": "xingcheng",
                "cache_storage": "local-model/runtime/cache/companions/star-chat",
                "backup_owner": "xingcheng",
                "backup_storage": "global-cleaner/data/business/backups/xingcheng",
                "separate_model_service": False,
                "separate_settings_layer": False,
                "separate_business_layer": False,
            },
            "star_ready": True,
            "star_version": "1.0",
            "analysis_model_count": len(ANALYSIS_MODEL_KEYS),
            "mathematical_capability_count": len(
                self._repository_for(
                    self.models.MATHEMATICAL
                ).mathematical_capability_catalog()
            ),
            "semantic_planning": "multi-intent-entity-and-document-aware",
            "generative_language_model": self.native_model.training_status(),
            "transformer_runtime": transformer_status,
            "llm": {
                "engine": "ollama",
                "available": llm_ready,
                "state": "READY" if llm_ready else "RECOVERING",
                "available_models": transformer_status.get("selectable_models") or [],
            },
            "rag": {
                "engine": str(rag_status.get("engine") or "local-vector-degraded-cache"),
                "canonical_engine": "qdrant",
                "available": bool(rag_status.get("available")),
                "state": str(rag_status.get("state") or ("READY" if rag_status.get("available") else "DEGRADED")),
            },
            "transformer_training_database": (
                self.transformer_training_repository.database_status()
            ),
            "self_training": "continuous-verified-self-distillation",
            "internal_ollama_training": {
                "enabled": True,
                "external_entry": False,
                "external_ai_used": False,
                "owner": self.NATIVE_MODEL_ID,
                "interval_seconds": self.INTERNAL_TRAINING_INTERVAL_SECONDS,
                "quality_gate_required": True,
                "automatic_database_update": True,
                "running": bool(
                    self._internal_training_task is not None
                    and not self._internal_training_task.done()
                ),
                "latest": dict(self._latest_internal_training),
            },
            "self_maintenance": dict(self._latest_self_maintenance),
            "coding_capability": self.coding_expert.__class__.__name__,
            "reading_capability": self.reading_expert.__class__.__name__,
            "module_architecture": {
                "mode": "automatic-composable-modules",
                "module_count": len(self.modules.MODULES),
            },
            "external_research": {
                "configured": False,
                "enabled": False,
                "fail_closed": True,
                "policy": "local-ollama-only",
            },
            "memory_review_required": True,
            "runtime_metrics": dict(self._runtime_metrics),
        }
