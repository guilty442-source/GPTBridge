from __future__ import annotations

import asyncio
from typing import Any


class CommandChannelsMixin:
    async def _handle_git(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_git_status":
            return "xingcheng_git_status_result", await asyncio.to_thread(
                self.git_repository.status
            )
        if command == "xingcheng_git_history":
            return "xingcheng_git_history_result", await asyncio.to_thread(
                self.git_repository.history, limit=int(payload.get("limit") or 20)
            )
        if command == "xingcheng_git_stage":
            confirmed = bool(
                payload.get("confirmed") is True
                or self._git_textual_confirmation(payload)
            )
            return "xingcheng_git_stage_result", await asyncio.to_thread(
                self.git_repository.stage,
                payload.get("paths"),
                confirmed=confirmed,
            )
        if command == "xingcheng_git_commit":
            confirmed = bool(
                payload.get("confirmed") is True
                or self._git_textual_confirmation(payload)
            )
            return "xingcheng_git_commit_result", await asyncio.to_thread(
                self.git_repository.commit,
                payload.get("message"),
                confirmed=confirmed,
                amend=bool(payload.get("amend") is True),
            )

    async def _handle_platform(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_platform_status":
            git_status, rag_status = await asyncio.gather(
                asyncio.to_thread(self.git_repository.status),
                asyncio.to_thread(self.local_rag.status),
            )
            return "xingcheng_platform_status_result", {
                "ok": True,
                "manager": "星澄 Core / Orchestrator",
                "highest_authority_management_required": True,
                "central_management_responsibilities": list(
                    self.CENTRAL_MANAGEMENT_RESPONSIBILITIES
                ),
                "governance_source": self._governance_source_status(),
                "fully_local": True,
                "execution_authority": False,
                "execution_owner": "governed-executor",
                "self_model_data_exception": "read-write",
                "git": git_status,
                "sql": {
                    "engine": "local-sqlite3-degraded",
                    "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
                    "canonical_central_engine": "postgresql",
                    "authority": "non-canonical-reconciliation-required",
                    "reconciliation_required": True,
                },
                "rag": rag_status,
                "llm": {"engine": "ollama", "role": "local-understanding-reasoning-and-operations"},
            }

    async def _handle_rag(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_rag_status":
            return "xingcheng_rag_status_result", await asyncio.to_thread(
                self.local_rag.status
            )
        if command == "xingcheng_rag_ingest":
            return "xingcheng_rag_ingest_result", await asyncio.to_thread(
                self.local_rag.ingest, dict(payload)
            )
        if command == "xingcheng_rag_query":
            return "xingcheng_rag_query_result", await asyncio.to_thread(
                self.local_rag.query, dict(payload)
            )
        if command == "xingcheng_knowledge_unified_search":
            return (
                "xingcheng_knowledge_unified_search_result",
                await self.local_knowledge.unified_search(
                    str(payload.get("query") or payload.get("question") or ""),
                    limit=int(payload.get("limit") or 8),
                ),
            )

    async def _handle_sql(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_sql_status":
            return "xingcheng_sql_status_result", await self.local_knowledge.sql_status()
        if command == "xingcheng_sql_get_personality":
            return "xingcheng_sql_get_personality_result", await self.local_knowledge.sql_get_personality()
        if command == "xingcheng_sql_save_personality":
            confirmed = bool(payload.get("confirmed") is True)
            return (
                "xingcheng_sql_save_personality_result",
                await self.local_knowledge.sql_save_personality(
                    dict(payload.get("personality") or payload.get("value") or {}),
                    confirmed=confirmed,
                ),
            )
        if command == "xingcheng_sql_list_knowledge":
            return (
                "xingcheng_sql_list_knowledge_result",
                await self.local_knowledge.sql_list_knowledge(
                    knowledge_type=str(payload.get("knowledge_type") or ""),
                    limit=int(payload.get("limit") or 100),
                ),
            )
        if command == "xingcheng_sql_save_knowledge":
            confirmed = bool(payload.get("confirmed") is True)
            return (
                "xingcheng_sql_save_knowledge_result",
                await self.local_knowledge.sql_save_knowledge(dict(payload), confirmed=confirmed),
            )

    async def _handle_diagnostics(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_diagnose_fault":
            symptom = str(
                payload.get("symptom")
                or payload.get("question")
                or payload.get("prompt")
                or payload.get("instruction")
                or ""
            )
            result = await asyncio.to_thread(
                self.fault_diagnostics.diagnose, symptom
            )
            return "xingcheng_diagnose_fault_result", result

    async def _handle_status(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_status":
            requested_mode = str(payload.get("prepare_mode") or "").strip().casefold()
            mode_preparation: dict[str, Any] | None = None
            if requested_mode in {"chat", "coding"} and self.transformer_runtime.enabled:
                preload_model = (
                    self.transformer_runtime.FRONTEND_WORKER_MODEL
                    if requested_mode == "coding"
                    else self.transformer_runtime.MODEL
                )
                keep_alive: int | str = "5m" if requested_mode == "coding" else -1
                try:
                    loaded = await asyncio.to_thread(
                        self.transformer_runtime.resource_manager.preload_model,
                        preload_model,
                        keep_alive=keep_alive,
                        device="gpu",
                    )
                    mode_preparation = {
                        "ok": True,
                        "mode": requested_mode,
                        "model": preload_model,
                        "resident": loaded.get("loaded") is True,
                    }
                except (OSError, RuntimeError, ValueError) as error:
                    mode_preparation = {
                        "ok": False,
                        "mode": requested_mode,
                        "model": preload_model,
                        "error": str(error),
                    }
            database = self.investment_repository.database_status()
            databases = {
                profile.role: self._repository_for(profile).database_status()
                for profile in self.models.profiles
            }
            evaluation = self._evaluate_upgrade(databases)
            result = {
                "ok": True,
                "mode_preparation": mode_preparation,
                "platform_mode": self.PLATFORM_MODE,
                "entry_gateway": self.ENTRY_GATEWAY,
                "platform_permission_profile": self.PLATFORM_PERMISSION_PROFILE,
                "platform_labels": [dict(item) for item in self.PLATFORM_LABELS],
                "star_native_model_permissions": dict(
                    self.STAR_NATIVE_MODEL_PERMISSIONS
                ),
                "platform_concurrency": "asynchronous-service-isolated",
                "platform_services": dict(self.PLATFORM_SERVICES),
                "main_system_companion_tools": ["star-chat"],
                "llm": {
                    "engine": "ollama",
                    "available": bool(self.transformer_runtime.status().get("available")),
                    "state": "READY" if self.transformer_runtime.status().get("available") else "RECOVERING",
                },
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
                "name": "星澄",
                "model": self.model,
                "status": (
                    "本機 Ollama 多模型路由已就緒，快速入口為 Gemma 4 E2B QAT，並依速度、推理與能力強度分工。"
                    if self.transformer_runtime.status().get("model_installed")
                    else "星澄統計式安全回退模型已就緒；本機 Transformer 尚未可用。"
                ),
                "model_version": self.native_model.VERSION,
                "model_architecture": (
                    "governed-local-multi-model-transformer+deterministic-specialists+"
                    "statistical-safety-fallback"
                ),
                "model_mode": "governed-local-transformer-llm",
                "generative_ai": True,
                "self_training": {
                    "mode": "continuous-verified-self-distillation",
                    "training_coordinator_model": "gemma4:e2b-it-qat",
                    "quality_gate_required": True,
                    "ollama_training": {
                        "enabled": True,
                        "transport": "ollama-loopback-only",
                        "external_entry": False,
                        "external_ai_used": False,
                        "internal_owner": self.NATIVE_MODEL_ID,
                        "automatic": True,
                        "interval_seconds": self.INTERNAL_TRAINING_INTERVAL_SECONDS,
                        "training_models": list(self.ollama_repositories),
                        "candidate_only": True,
                        "direct_model_database_write": False,
                        "star_native_database_write_after_quality_gate": True,
                        "automatic_database_update": True,
                        "latest": dict(self._latest_internal_training),
                        "direct_weight_access": False,
                        "star_quality_gate_required": True,
                        "maximum_examples_per_request": self.ollama_training_gate.MAX_EXAMPLES,
                    },
                    "models": {
                        profile.model_id: self.model_engines.for_profile(
                            profile
                        ).training_status()
                        for profile in self.models.profiles
                    },
                },
                "model_engines": self.model_engines.status(),
                "transformer_runtime": self.transformer_runtime.status(),
                "ollama_model_databases": {
                    model_id: repository.status()
                    for model_id, repository in self.ollama_repositories.items()
                },
                "transformer_training_database": (
                    self.transformer_training_repository.database_status()
                ),
                "module_architecture": {
                    "mode": "automatic-composable-modules",
                    "modules": self.modules.catalog(),
                },
                "self_maintenance": dict(self._latest_self_maintenance),
                "understanding": {
                    "schema": "star-semantic-plan/v1",
                    "language_priority": "traditional-chinese-taiwan-first",
                    "english_intermediate_representation_used": False,
                    "features": [
                        "original-traditional-chinese-preservation",
                        "taiwan-chinese-synonym-normalization",
                        "colloquial-ellipsis-typo-and-mixed-language-input",
                        "active-verb-and-operation-object-detection",
                        "parameter-and-specific-constraint-extraction",
                        "conversation-context-completion",
                        "destructive-ambiguity-safety-confirmation",
                        "multi-intent-planning",
                        "question-type-detection",
                        "negation-detection",
                        "constraint-extraction",
                        "requested-output-detection",
                        "keyword-extraction",
                        "date-percentage-money-url-email-entities",
                        "mixed-language-detection",
                    ],
                    "document_aware": True,
                },
                "autonomous_agent": {
                    "enabled": True,
                    "star_native_model_included": False,
                    "default_in_model_dialogue": True,
                    "mode": "bounded-plan-execute-verify-recover",
                    "project_scope": "all-project-source-excluding-governance-rule",
                    "workflow_sequence": [
                        *self.AUTOMATIC_WORKFLOW_SEQUENCE,
                    ],
                    "understanding_authority": {
                        "primary": "qwen3.5:9b-q4_K_M",
                        "backup": "nemotron-3-nano:4b",
                    },
                    "allocation_authority": self.GENERALIST_COORDINATOR_MODEL,
                    "integration_authority": self.FINAL_COORDINATOR_MODEL,
                    "execution_authority": self.CODING_EXPERT_MODEL,
                    "inspection_authority": self.RELEASE_REVIEW_MODEL,
                    "result_authority": self.FINAL_COORDINATOR_MODEL,
                    "backup_policy": "none",
                    "failure_adjudicator": self.COMMAND_UNDERSTANDING_MODEL,
                    "commander_dynamic_reassignment": True,
                    "maximum_dynamic_reassignments": 1,
                    "maximum_model_concurrency": 1,
                    "database_write_scope": "all-project-databases-excluding-governance-rule",
                    "database_actions": list(
                        self.STAR_NATIVE_MODEL_PERMISSIONS["database_actions"]
                    ),
                    "external_ai_used": False,
                    "governance_rule_mutable": False,
                    "source_change_gates": [
                        "majority-vote",
                        "three-model-inspection",
                        "recoverable-backup",
                        "governance-audit",
                        "rollback-on-failure",
                    ],
                },
                "coding": {
                    "model_id": self.models.CODING.model_id,
                    "languages": sorted(self.coding_expert.ALLOWED_LANGUAGES),
                    "actions": sorted(self.coding_expert.ALLOWED_ACTIONS),
                    "python_artifacts": sorted(
                        self.coding_expert.ALLOWED_PYTHON_KINDS
                    ),
                    "script_artifacts": sorted(
                        self.coding_expert.ALLOWED_SCRIPT_KINDS
                    ),
                    "sql_policy": "parameterized-read-only-select",
                    "database_executor_actions": list(
                        self.STAR_NATIVE_MODEL_PERMISSIONS["database_actions"]
                    ),
                    "database_executor_scope": (
                        "all-project-databases-excluding-governance-rule"
                    ),
                    "static_security_scan": True,
                    "generated_test_support": True,
                    "self_upgrade_authoring": True,
                    "unified_diff_proposals": True,
                    "direct_source_write": False,
                    "publish_authority": "governance-versioned-release-only",
                },
                "reading": {
                    "engine": self.reading_expert.__class__.__name__,
                    "actions": sorted(self.reading_expert.SUPPORTED_ACTIONS),
                    "maximum_documents": self.reading_expert.MAX_DOCUMENTS,
                    "maximum_characters": self.reading_expert.MAX_TOTAL_CHARACTERS,
                    "source_offsets": True,
                    "citation_required": True,
                    "unsupported_answer_behavior": "explicit-insufficient-evidence",
                    "network_access": False,
                },
                "rag": self.local_rag.status(),
                "models": [
                    {
                        **model,
                        "database": databases[str(model["role"])],
                    }
                    for model in self.models.catalog()
                ],
                "model_router": "ollama-speed-reasoning-capability-router",
                "model_selection": "automatic",
                "manual_model_selection": False,
                "star_native_model_included": False,
                "external_ai_used": False,
                "project_scope": "all-project-source-excluding-governance-rule",
                "model_isolation": {
                    "enforcement": "mandatory",
                    "database": "one-exclusive-database-per-model",
                    "specialist_network": "disabled",
                    "specialist_external_ai": "disabled",
                    "specialist_peer_access": False,
                    "direct_specialist_access": False,
                },
                "orchestration": {
                    "workflow": [
                        *self.AUTOMATIC_WORKFLOW_SEQUENCE,
                    ],
                    "command_understanding_model": self.COMMAND_UNDERSTANDING_MODEL,
                    "task_allocation_model": "qwen3:30b-a3b-instruct-2507-q4_K_M",
                    "integration_model": "gpt-oss:20b",
                    "integration_backup": "qwen3:30b-a3b-instruct-2507-q4_K_M",
                    "execution_model": "qwen3.6:35b-a3b-coding",
                    "inspection_model": self.RELEASE_REVIEW_MODEL,
                    "result_model": "gemma4:e2b-it-qat",
                    "backup_policy": "none",
                    "failure_adjudicator": self.COMMAND_UNDERSTANDING_MODEL,
                    "commander_dynamic_reassignment": True,
                    "maximum_dynamic_reassignments": 1,
                    "majority_vote_models": [],
                    "majority_vote_required": 0,
                    "capability_composition_owner": self.NATIVE_MODEL_ID,
                    "llama_mode": "manual-only-no-vote",
                    "specialist_direct_access": False,
                    "specialist_peer_access": False,
                    "external_ai_used": False,
                    "star_native_model_included": False,
                    "capability_database_owner": self.NATIVE_MODEL_ID,
                },
                "memory_interoperability": self.memory_broker.status(),
                "external_model_used": self.transformer_runtime.enabled,
                "remote_model_used": False,
                "third_party_foundation_weights": self.transformer_runtime.enabled,
                "network_access": "public-investment-sources-read-only",
                "endpoint_scope": "loopback-local-transformer-runtime",
                "market_search": "public-web-read-only",
                "investment_analysis_owner": "星澄",
                "investment_model_roles": {
                    "selection_mode": "automatic-only",
                    "manual_model_override": False,
                    "primary_business_and_first_stage": self.DATA_COORDINATOR_MODEL,
                    "calculation_weight_risk_and_final_coordination": self.CODING_EXPERT_MODEL,
                    "dynamic_dispatch": {
                        "routine": self.DATA_COORDINATOR_MODEL,
                        "complex": self.GENERALIST_COORDINATOR_MODEL,
                        "medium_reasoning": self.MATHEMATICAL_REVIEW_MODEL,
                        "high_reasoning": self.RELEASE_REVIEW_MODEL,
                        "final": self.CODING_EXPERT_MODEL,
                    },
                },
                "external_research": {
                    "configured": False,
                    "enabled": False,
                    "fail_closed": True,
                    "policy": "local-ollama-only",
                    "external_ai_used": False,
                    "transport": "disabled",
                    "queue_when_offline": False,
                },
                "database": database,
                "databases": databases,
                "investment_expert": {
                    "network_policy": "disabled",
                    "parameter_count": int(
                        database["tables"].get("investment_parameter_definition") or 0
                    ),
                    "model_count": int(
                        database["tables"].get("investment_model_definition") or 0
                    ),
                    "models": self.investment_repository.investment_model_catalog(),
                    "adjustable_parameters": self.investment_repository.investment_parameter_values(),
                    "parameter_advisor": "chatgpt",
                    "parameter_applier": "star-main-native-model",
                },
                "mathematical_expert": {
                    "network_policy": "disabled",
                    "capability_count": int(
                        databases["mathematical-reasoning-specialist"]["tables"].get(
                            "mathematical_capability_definition"
                        )
                        or 0
                    ),
                    "capabilities": self._repository_for(
                        self.models.MATHEMATICAL
                    ).mathematical_capability_catalog(),
                },
                "upgrade_optimization": evaluation,
                "capability_evaluation": evaluation.get("capability_evaluation", {}),
            }
            return "xingcheng_status_result", result
