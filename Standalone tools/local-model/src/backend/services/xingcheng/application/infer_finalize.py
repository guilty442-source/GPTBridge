from __future__ import annotations

import asyncio
from typing import Any


class InferFinalizeMixin:

    async def _infer_finalize(
        self,
        output: dict[str, Any],
        payload: dict[str, Any],
        profile: Any,
        attempted_profile: Any,
        planned_intents: list[str],
        native_model_requested: bool,
        requested_runtime_model: str,
        prompt: str,
        inference_payload: dict[str, Any],
        business_scope: str,
    ) -> tuple[str, dict[str, Any]]:
        market_research = output.get("market_research")
        if isinstance(market_research, dict):
            await asyncio.to_thread(
                self._repository_for(self.models.MAIN).record_market_search,
                {
                    "holdings": payload.get("holdings") or [],
                    "origin": profile.model_id,
                },
                market_research,
            )
        training_candidate = output.pop("_training_candidate", None)
        output["self_training"] = self._apply_self_training(
            attempted_profile,
            training_candidate if isinstance(training_candidate, dict) else {},
        )
        output["module_execution"] = self.modules.execution_report(
            planned_intents,
            coordinator_model=(
                self.NATIVE_MODEL_ID
                if native_model_requested
                else self.FINAL_COORDINATOR_MODEL
            ),
            output=output,
        )
        transformer_used = output.get("mode") == "governed-local-transformer-llm"
        if native_model_requested or (
            not requested_runtime_model and not transformer_used
        ):
            self._repository_for(profile).record(
                profile.model_id,
                {"prompt": str(payload.get("prompt") or "")},
                output,
            )
        persistence_requested = native_model_requested and self._native_persistence_requested(
            prompt, inference_payload
        )
        remembered = (
            self.memory_broker.remember_internal_task(
                profile,
                business_scope=business_scope,
                prompt=prompt,
                result=output,
            )
            if persistence_requested
            or (not requested_runtime_model and not transformer_used)
            else []
        )
        output["memory_interoperability"] = {
            "mode": "star-mediated-copy",
            "stored_count": len(remembered),
            "persistence_requested": persistence_requested,
            "platform_validated": persistence_requested and bool(remembered),
            "external_direct_write": False,
        }
        return "xingcheng_infer_result", output

    def _infer_reconcile_model_selection(
        self,
        output: dict[str, Any],
        profile: Any,
        planned_intent: str,
        native_model_requested: bool,
        requested_runtime_model: str,
    ) -> tuple[Any, Any]:
        attempted_profile = profile
        fallback_reason = ""
        resolved_intent = str(output.get("intent") or planned_intent)
        if profile == self.models.INVESTMENT and resolved_intent in {"analysis", "risk"}:
            if output.get("analysis") is None:
                fallback_reason = "investment-input-required"
        elif profile == self.models.MATHEMATICAL:
            mathematical_result = output.get("mathematical_result")
            expected_key = {
                "calculation": "calculation",
                "statistics": "statistics",
                "data_organization": "data_organization",
            }.get(resolved_intent)
            if expected_key and not (
                isinstance(mathematical_result, dict)
                and isinstance(mathematical_result.get(expected_key), dict)
                and not mathematical_result[expected_key].get("error")
            ):
                fallback_reason = "mathematical-input-or-capability-required"
        elif profile == self.models.CODING:
            coding_result = output.get("coding_result")
            if not (
                isinstance(coding_result, dict)
                and coding_result.get("ok") is True
            ):
                fallback_reason = "coding-specification-or-validation-required"
        if fallback_reason:
            self._runtime_metrics["model_route_fallback_count"] = int(
                self._runtime_metrics["model_route_fallback_count"]
            ) + 1
            profile = self.models.primary
            output["specialist_fallback"] = {
                "used": True,
                "attempted_model": attempted_profile.model_id,
                "fallback_model": self.models.primary.model_id,
                "reason": fallback_reason,
                "cross_specialist_fallback": False,
            }
        else:
            output["specialist_fallback"] = {"used": False}
        self._identify_model(output, profile)
        if requested_runtime_model:
            output["model_selection"] = "user-selected"
            output["manual_model_selection"] = True
            output["selected_runtime_model"] = requested_runtime_model
        if native_model_requested:
            output["model"] = self.NATIVE_MODEL_ID
            output["model_name"] = "星澄"
            output["model_role"] = "unified-native-local-model"
            output["coordinator_model"] = self.NATIVE_MODEL_ID
            output["coordination"] = "native-model-direct"
            output["delegated"] = False
            output["permission_scope"] = dict(
                self.STAR_NATIVE_MODEL_PERMISSIONS
            )
            output["native_database_access"] = {
                "enabled": True,
                "trigger": "explicit-user-selected-star-native-model",
                "owner_model_id": self.NATIVE_MODEL_ID,
                "database_scope": "all-project-databases-excluding-governance-rule",
                "default_operational_database": "main",
                "access_reason": "user-request-context-and-continuity",
                "actions": list(
                    self.STAR_NATIVE_MODEL_PERMISSIONS["database_actions"]
                ),
                "read": "all-project-databases-via-governed-platform",
                "write": "all-project-databases-via-governed-platform",
                "specialist_database_access": True,
                "investment_database_access": True,
                "ollama_model_database_access": True,
                "project_database_scope": "all-project-databases-excluding-governance-rule",
                "governance_rule_excluded": True,
            }
        return profile, attempted_profile
