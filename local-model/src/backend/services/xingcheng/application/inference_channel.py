from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from .investment_analysis import analyze_investments


class InferenceChannelMixin:
    async def _handle_upgrade_memory(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_evaluate_upgrade":
            result = self._evaluate_upgrade()
            return "xingcheng_evaluate_upgrade_result", result
        if command == "xingcheng_memory_list":
            records = self.memory_broker.list_memories(
                include_inactive=payload.get("include_inactive") is True,
                limit=int(payload.get("limit") or 100),
            )
            return "xingcheng_memory_list_result", {
                "ok": True,
                "records": records,
                "count": len(records),
                "review_policy": "external-candidates-require-approval",
                "direct_external_write": False,
            }
        if command == "xingcheng_memory_review":
            try:
                reviewed = self.memory_broker.review_memory(
                    str(payload.get("memory_id") or ""),
                    action=str(payload.get("action") or ""),
                    reviewer=str(payload.get("reviewer") or "star-owner"),
                    reason=str(payload.get("reason") or ""),
                )
            except (KeyError, ValueError) as exc:
                return "xingcheng_memory_review_result", {
                    "ok": False,
                    "error_code": "MEMORY_REVIEW_REJECTED",
                    "message": str(exc),
                }
            return "xingcheng_memory_review_result", {
                "ok": True,
                "reviewed": reviewed,
                "review_count": len(reviewed),
                "governance_checked": True,
            }

    async def _handle_infer(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command != "xingcheng_infer":
            raise ValueError(f"unsupported local AI command: {command}")
        gate_error, requested_runtime_model, native_model_requested, direct_runtime_model = (
            await self._infer_validate_request(payload)
        )
        if gate_error is not None:
            return gate_error
        build_error, prompt, raw_command, command_plan, progress_callback, command_understanding_model = (
            self._infer_build_command_plan(payload)
        )
        if build_error is not None:
            return build_error
        preflight_error = await self._infer_run_command_preflight_gates(
            payload,
            command_plan,
            raw_command,
            prompt,
            native_model_requested,
            progress_callback,
        )
        if preflight_error is not None:
            return preflight_error
        inference_payload, prompt, planned_intents, planned_intent, external_tasks = (
            await self._infer_prepare_intents(payload, command_plan, raw_command)
        )
        session_error, automatic_runtime_model, planned_profile, business_scope, assigned_model = (
            self._infer_plan_session_context(
                inference_payload,
                planned_intent,
                prompt,
                direct_runtime_model,
                native_model_requested,
                command,
            )
        )
        if session_error is not None:
            return session_error
        generate_error, output = await self._infer_generate_model_output(
            inference_payload,
            prompt,
            planned_intent,
            planned_profile,
            native_model_requested,
        )
        if generate_error is not None:
            return generate_error
        profile = (
            self.models.MAIN
            if native_model_requested
            else self.models.for_command(
                command, intent=str(output.get("intent") or "")
            )
        )
        self._infer_scheduling_envelopes(
            output, inference_payload, assigned_model, planned_intents
        )
        autonomous_agent = inference_payload.get("autonomous_agent") is not False
        semantic_understanding = output.get("semantic_understanding")
        command_understanding = (
            semantic_understanding.get("comprehension", {}).get("command", {})
            if isinstance(semantic_understanding, dict)
            and isinstance(semantic_understanding.get("comprehension"), dict)
            else {}
        )
        output["autonomous_agent"] = self._infer_autonomous_agent_envelope(
            inference_payload,
            command_plan,
            planned_intents,
            command_understanding_model,
            command_understanding,
            autonomous_agent,
        )
        if inference_payload.get("entry_mode") == "user-command":
            output["user_command"] = self._infer_user_command_envelope(
                inference_payload,
                planned_intent,
                prompt,
                command_understanding,
                command_plan,
                raw_command,
                assigned_model,
                command_understanding_model,
                autonomous_agent,
            )
        output["external_collaboration_plan"] = {
            "enabled": False,
            "policy": "local-ollama-only",
            "external_ai_used": False,
            "tasks": external_tasks,
        }
        await self._infer_apply_specialists(
            output,
            inference_payload,
            profile,
            planned_intent,
            native_model_requested,
            prompt,
        )
        profile, attempted_profile = self._infer_reconcile_model_selection(
            output,
            profile,
            planned_intent,
            native_model_requested,
            requested_runtime_model,
        )
        self._infer_instruction_execution(
            output, inference_payload, native_model_requested, planned_intent
        )
        self._infer_propagate_execution_state(output)
        resolved_intent = str(output.get("intent") or planned_intent)
        pipeline_result = await self._infer_transformer_pipeline(
            output,
            inference_payload,
            prompt,
            planned_intents,
            native_model_requested,
            resolved_intent,
            profile,
            attempted_profile,
            direct_runtime_model,
            automatic_runtime_model,
        )
        if pipeline_result is not None:
            return pipeline_result
        if resolved_intent == "reading":
            output["transformer_inference"] = {
                "ok": True,
                "used": False,
                "reason": "source-attributed-extractive-reading-preserved",
            }
        return await self._infer_finalize(
            output,
            payload,
            profile,
            attempted_profile,
            planned_intents,
            native_model_requested,
            requested_runtime_model,
            prompt,
            inference_payload,
            business_scope,
        )



    async def _infer_apply_specialists(
        self,
        output: dict[str, Any],
        inference_payload: dict[str, Any],
        profile: Any,
        planned_intent: str,
        native_model_requested: bool,
        prompt: str,
    ) -> None:
        if profile == self.models.MATHEMATICAL:
            output["mathematical_result"] = self.mathematical_expert.process(
                inference_payload, str(output.get("intent") or planned_intent)
            )
        if str(output.get("intent") or planned_intent) == "reading":
            reading_result = self.reading_expert.process(inference_payload)
            output["reading_result"] = reading_result
            if reading_result.get("ok") is True:
                reading_response = str(reading_result.get("response") or "")
                reading_token_count = len(
                    self.native_model.runtime.language_model.tokenize(reading_response)
                )
                output["response"] = reading_response
                generation = output.get("generation")
                if not isinstance(generation, dict):
                    generation = {}
                generation.update(
                    {
                        "text": reading_response,
                        "token_count": reading_token_count,
                        "facts_preserved": True,
                        "grounding": "supplied-document-citations",
                        "reading_extractive_grounding": True,
                    }
                )
                output["generation"] = generation
                output["evidence"] = list(reading_result.get("citations") or [])
                semantic_understanding = output.get("semantic_understanding")
                if isinstance(semantic_understanding, dict):
                    semantic_understanding["document_understanding"] = dict(
                        reading_result.get("metrics") or {}
                    )
                context_retrieval = output.get("context_retrieval")
                if isinstance(context_retrieval, dict):
                    context_retrieval["document_chunk_count"] = int(
                        (reading_result.get("metrics") or {}).get("chunk_count") or 0
                    )
                    context_retrieval["supplied_documents_only"] = True
                evidence_policy = output.get("evidence_policy")
                if isinstance(evidence_policy, dict):
                    evidence_policy["verbatim_citation_offsets_required"] = True
                    evidence_policy["unsupported_reading_answers_rejected"] = True
                evidence_sufficient = reading_result.get("evidence_sufficient") is True
                output["_training_candidate"] = {
                    "intent": "reading",
                    "input_text": prompt,
                    "target_text": reading_response,
                    "source_type": "source-attributed-reading",
                    "quality_score": 0.95,
                    "validated": bool(
                        evidence_sufficient and 8 <= reading_token_count <= 360
                    ),
                    "validation": {
                        **dict(reading_result.get("quality") or {}),
                        "evidence_sufficient": evidence_sufficient,
                        "bounded_output": 8 <= reading_token_count <= 360,
                    },
                }
            else:
                reading_message = str(
                    reading_result.get("message")
                    or "請提供需要閱讀的文件內容。"
                )
                output["response"] = reading_message
                generation = output.get("generation")
                if not isinstance(generation, dict):
                    generation = {}
                generation.update(
                    {
                        "text": reading_message,
                        "token_count": len(
                            self.native_model.runtime.language_model.tokenize(
                                reading_message
                            )
                        ),
                        "facts_preserved": True,
                        "reading_extractive_grounding": False,
                        "grounding_fallback_reason": "reading-content-required",
                    }
                )
                output["generation"] = generation
                output["_training_candidate"] = {
                    "intent": "reading",
                    "input_text": prompt,
                    "target_text": reading_message,
                    "source_type": "reading-input-required",
                    "quality_score": 0.0,
                    "validated": False,
                    "validation": {"reading_content_supplied": False},
                }
        if profile == self.models.CODING:
            active_intent = str(output.get("intent") or planned_intent)
            if active_intent == "self_upgrade" and not native_model_requested:
                output["self_repair"] = await asyncio.to_thread(
                    self._execute_self_repair_command
                )
            coding_result = self.coding_expert.process(
                inference_payload, active_intent
            )
            proposal = coding_result.get("upgrade_proposal")
            if (
                not native_model_requested
                and isinstance(proposal, dict)
                and proposal.get("proposal_ready") is True
            ):
                target = proposal.get("target")
                stored_proposal = self._repository_for(
                    self.models.CODING
                ).store_code_upgrade_proposal(
                    target_path=str(
                        target.get("path") if isinstance(target, dict) else ""
                    ),
                    language=str(coding_result.get("language") or ""),
                    source_text=str(coding_result.get("source") or ""),
                    validation=dict(coding_result.get("validation") or {}),
                )
                proposal["persistence"] = stored_proposal
            output["coding_result"] = coding_result


    def _infer_instruction_execution(
        self,
        output: dict[str, Any],
        inference_payload: dict[str, Any],
        native_model_requested: bool,
        planned_intent: str,
    ) -> None:
        instruction_execution = output.get("instruction_execution")
        if isinstance(instruction_execution, dict):
            instruction_execution["coordinator_model"] = (
                self.NATIVE_MODEL_ID
                if native_model_requested
                else self.FINAL_COORDINATOR_MODEL
            )
            instruction_execution["assigned_model"] = (
                self.NATIVE_MODEL_ID
                if native_model_requested
                else self._ollama_model_for_intent(
                    str(output.get("intent") or planned_intent)
                )
            )
            instruction_execution["delegated"] = not native_model_requested
            intent = str(output.get("intent") or "")
            executed = True
            missing_inputs: list[str] = []
            if intent == "search" and not isinstance(output.get("market_research"), dict):
                executed = False
                missing_inputs.append("holdings")
            elif intent in {"analysis", "risk"} and output.get("analysis") is None:
                executed = False
                missing_inputs.append("holdings")
            elif intent == "statistics" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "statistics" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("numbers")
            elif intent == "data_organization" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "data_organization" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("records")
            elif intent == "calculation" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "calculation" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("expression")
            elif intent == "self_upgrade":
                self_repair = output.get("self_repair")
                executed = bool(
                    isinstance(self_repair, dict)
                    and self_repair.get("executed") is True
                )
                if not executed:
                    missing_inputs.append("self-repair-execution")
            elif intent == "coding" and not (
                isinstance(output.get("coding_result"), dict)
                and output["coding_result"].get("ok") is True
            ):
                executed = False
                missing_inputs.append("valid-code-spec")
            elif intent == "reading" and not (
                isinstance(output.get("reading_result"), dict)
                and output["reading_result"].get("ok") is True
            ):
                executed = False
                missing_inputs.append("document-text-or-documents")
            elif intent == "visual" and not any(
                isinstance(inference_payload.get(key), list)
                and bool(inference_payload.get(key))
                for key in ("images", "video_frames", "document_images")
            ):
                executed = False
                missing_inputs.append(
                    "images-or-video-frames-or-document-images"
                )
            instruction_execution["executed"] = executed
            instruction_execution["status"] = "completed" if executed else "input-required"
            instruction_execution["missing_inputs"] = missing_inputs


    def _infer_apply_transformer_success(
        self,
        output: dict[str, Any],
        transformer_result: dict[str, Any],
        resolved_intent: str,
        direct_runtime_model: str,
        profile: Any,
    ) -> None:
        transformer_text = str(transformer_result.get("text") or "").strip()
        if resolved_intent == "self_upgrade" and isinstance(
            output.get("self_repair"), dict
        ):
            repair = output["self_repair"]
            completed = repair.get("status") == "completed"
            execution_summary = (
                "已執行星澄自我檢討與維護；模型、學習資料庫、能力與治理健康檢查均已完成。"
                if completed
                else "已執行星澄自我檢討與維護；仍有項目需要進一步處理。"
            )
            transformer_text = f"{execution_summary}\n\n{transformer_text}"
        output["response"] = transformer_text
        generation = output.get("generation")
        if not isinstance(generation, dict):
            generation = {}
        generation.update(
            {
                "text": transformer_text,
                "token_count": int(transformer_result.get("eval_count") or 0),
                "decoder": transformer_result["decoder"],
                "model_type": "quantized-local-decoder-transformer",
                "model": transformer_result["model"],
                "model_family": transformer_result["model_family"],
                "parameter_class": transformer_result["parameter_class"],
                "parameter_count": transformer_result["parameter_count"],
                "quantization": transformer_result["quantization"],
                "context_window": transformer_result["context_window"],
                "facts_preserved": transformer_result["facts_supported"],
                "facts_supported": transformer_result["facts_supported"],
                "transformer_fallback_used": False,
            }
        )
        output["generation"] = generation
        output["mode"] = "governed-local-transformer-llm"
        output["architecture"] = (
            "governed-selectable-local-decoder-transformer+"
            "deterministic-specialists+statistical-safety-fallback"
        )
        output["external_model_used"] = True
        output["remote_model_used"] = False
        output["third_party_weights_used"] = True
        output["loopback_model_runtime_used"] = True
        output["foundation_model_license"] = transformer_result[
            "foundation_model_license"
        ]
        selected_ollama_model = str(
            transformer_result.get("model") or direct_runtime_model
        )
        output["model"] = selected_ollama_model
        output["model_name"] = str(
            transformer_result.get("model_family") or selected_ollama_model
        )
        output["model_role"] = (
            "user-selected-direct"
            if direct_runtime_model
            else profile.role
        )
        output["coordinator_model"] = (
            selected_ollama_model
            if direct_runtime_model
            else self.FINAL_COORDINATOR_MODEL
        )
        output["coordination"] = (
            "direct-selected-model"
            if direct_runtime_model
            else "local-ollama-priority-routing"
        )
        output["delegated"] = False
        output["star_native_model_used"] = False
        output["external_ai_used"] = False
        candidate = output.get("_training_candidate")
        if isinstance(candidate, dict):
            candidate["validated"] = False
            validation = candidate.get("validation")
            if not isinstance(validation, dict):
                validation = {}
            validation["foundation_model_output_excluded_from_self_training"] = True
            candidate["validation"] = validation


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


    def _infer_scheduling_envelopes(
        self,
        output: dict[str, Any],
        inference_payload: dict[str, Any],
        assigned_model: str,
        planned_intents: list[str],
    ) -> None:
        output["task_arrangement"] = {
            "mode": "traditional-chinese-first-governed-workflow",
            "task_allocation_model": self.GENERALIST_COORDINATOR_MODEL,
            "integration_model": self.FINAL_COORDINATOR_MODEL,
            "manual_assignment_allowed": False,
            "star_native_model_included": False,
            "external_ai_used": False,
            "project_scope": "all-project-source-excluding-governance-rule",
            "tasks": self._arrange_ollama_tasks(planned_intents),
        }
        scheduled_intensity = str(
            inference_payload.get("task_intensity") or "normal"
        ).strip().casefold()
        output["model_scheduling"] = {
            "automatic": True,
            "selected_model": assigned_model,
            "selection_dimensions": [
                "intent",
                "task_intensity",
                "reasoning_effort",
                "generation_speed",
                "installed_models",
                "residency",
            ],
            "rules_file": "config/繁體中文自動模式規則.json",
            "task_intensity": scheduled_intensity,
            "task_intensity_source": str(
                inference_payload.get("task_intensity_source")
                or "automatic-traditional-chinese-command-assessment"
            ),
            "policy": dict(
                self.transformer_runtime.TASK_INTENSITY_SCHEDULING.get(
                    scheduled_intensity,
                    self.transformer_runtime.TASK_INTENSITY_SCHEDULING["normal"],
                )
            ),
            "parallel_independent_subtasks": True,
            "sequential_dependent_stages": True,
            "model_escalation": "failure-only",
            "manual_model_selection_required": False,
        }


    def _infer_autonomous_agent_envelope(
        self,
        inference_payload: dict[str, Any],
        command_plan: dict[str, Any],
        planned_intents: list[str],
        command_understanding_model: str,
        command_understanding: dict[str, Any],
        autonomous_agent: bool,
    ) -> dict[str, Any]:
        return {
            "enabled": autonomous_agent,
            "star_native_model_included": False,
            "mode": "bounded-plan-execute-verify-recover",
            "project_scope": "all-project-source-excluding-governance-rule",
            "command_understood": bool(command_understanding.get("recognized")),
            "understanding_layer": "traditional-chinese-taiwan-first",
            "english_translation_before_intent": False,
            "semantic_plan_schema": "star-semantic-plan/v1",
            "task_classification": dict(
                command_plan.get("task_classification") or {}
            ),
            "task_intensity": dict(command_plan.get("task_intensity") or {}),
            "safety_gate": dict(command_plan.get("safety") or {}),
            "execution_requested": bool(
                command_understanding.get("execution_requested")
            ),
            "planned_intents": list(planned_intents),
            "planned_steps": self._arrange_ollama_tasks(planned_intents),
            "workflow_sequence": [
                *self.AUTOMATIC_WORKFLOW_SEQUENCE,
            ],
            "command_understanding_model": command_understanding_model,
            "planner_model": self.GENERALIST_COORDINATOR_MODEL,
            "integration_model": self.FINAL_COORDINATOR_MODEL,
            "executor_model": self.CODING_EXPERT_MODEL,
            "inspection_model": self.RELEASE_REVIEW_MODEL,
            "result_model": self.FINAL_COORDINATOR_MODEL,
            "backup_policy": "none",
            "failure_adjudicator": self.COMMAND_UNDERSTANDING_MODEL,
            "commander_dynamic_reassignment": True,
            "maximum_dynamic_reassignments": 1,
            "governance_checked": True,
            "governance_rule_mutable": False,
            "programming_project_root": str(
                inference_payload.get("programming_folder") or ""
            ),
            "programming_folder": str(
                inference_payload.get("programming_folder") or ""
            ),
            "outside_programming_scope_allowed": False,
            "database_write_performed": False,
            "status": "executing" if autonomous_agent else "disabled",
        }


    def _infer_user_command_envelope(
        self,
        inference_payload: dict[str, Any],
        planned_intent: str,
        prompt: str,
        command_understanding: dict[str, Any],
        command_plan: dict[str, Any],
        raw_command: str,
        assigned_model: str,
        command_understanding_model: str,
        autonomous_agent: bool,
    ) -> dict[str, Any]:
        return {
                "entry": "model-dialogue",
                "accepted": True,
                "command": str(inference_payload.get("user_command") or prompt)[:32_000],
                "intent": planned_intent,
                "understood": bool(command_understanding.get("recognized")),
                "understanding_layer": "traditional-chinese-taiwan-first",
                "normalized_command": str(
                    command_plan.get("normalized_input") or raw_command
                )[:32_000],
                "actions": list(command_plan.get("actions") or []),
                "operation_objects": list(
                    command_plan.get("operation_objects") or []
                ),
                "parameters": dict(command_plan.get("parameters") or {}),
                "constraints": list(
                    command_plan.get("specific_constraints") or []
                ),
                "context_completion": dict(
                    command_plan.get("context_completion") or {}
                ),
                "task_classification": dict(
                    command_plan.get("task_classification") or {}
                ),
                "task_intensity": dict(
                    command_plan.get("task_intensity") or {}
                ),
                "safety_gate": dict(command_plan.get("safety") or {}),
                "execution_requested": bool(
                    command_understanding.get("execution_requested")
                ),
                "assigned_model": assigned_model,
                "command_understanding_model": command_understanding_model,
                "planner_model": self.GENERALIST_COORDINATOR_MODEL,
                "integration_model": self.FINAL_COORDINATOR_MODEL,
                "executor_model": self.CODING_EXPERT_MODEL,
                "inspection_model": self.RELEASE_REVIEW_MODEL,
                "result_model": self.DATA_COORDINATOR_MODEL,
                "project_scope": "all-project-source-excluding-governance-rule",
                "governance_checked": True,
                "status": "executing" if autonomous_agent else "planned",
        }


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


    def _infer_propagate_execution_state(
        self,
        output: dict[str, Any],
    ) -> None:
        agent_state = output.get("autonomous_agent")
        if isinstance(agent_state, dict) and agent_state.get("enabled") is True:
            execution_state = output.get("instruction_execution")
            execution_status = (
                str(execution_state.get("status") or "completed")
                if isinstance(execution_state, dict)
                else "completed"
            )
            agent_state["status"] = execution_status
            agent_state["command_executed"] = bool(
                isinstance(execution_state, dict)
                and execution_state.get("executed") is True
            )
            agent_state["missing_inputs"] = (
                list(execution_state.get("missing_inputs") or [])
                if isinstance(execution_state, dict)
                else []
            )
        command_state = output.get("user_command")
        if isinstance(command_state, dict):
            execution_state = output.get("instruction_execution")
            command_state["status"] = (
                str(execution_state.get("status") or "completed")
                if isinstance(execution_state, dict)
                else "completed"
            )
            command_state["executed"] = bool(
                isinstance(execution_state, dict)
                and execution_state.get("executed") is True
            )
            command_state["missing_inputs"] = (
                list(execution_state.get("missing_inputs") or [])
                if isinstance(execution_state, dict)
                else []
            )


    async def _infer_transformer_pipeline(
        self,
        output: dict[str, Any],
        inference_payload: dict[str, Any],
        prompt: str,
        planned_intents: list[str],
        native_model_requested: bool,
        resolved_intent: str,
        profile: Any,
        attempted_profile: Any,
        direct_runtime_model: str,
        automatic_runtime_model: str,
    ) -> tuple[str, dict[str, Any]] | None:
        if (
            self.transformer_runtime.enabled
            and resolved_intent != "reading"
            and (
                not native_model_requested
                or resolved_intent
                in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
            )
        ):
            task_intensity = str(
                inference_payload.get("task_intensity") or ""
            ).strip().casefold()
            intensity_controlled = task_intensity in {
                "simple",
                "normal",
                "intermediate",
                "difficult",
            }
            transformer_started = time.perf_counter()
            self._runtime_metrics["transformer_request_count"] = int(
                self._runtime_metrics["transformer_request_count"]
            ) + 1
            visual_inputs: list[Any] = []
            for visual_key in ("images", "video_frames", "document_images"):
                supplied_visuals = inference_payload.get(visual_key)
                if isinstance(supplied_visuals, list):
                    visual_inputs.extend(supplied_visuals)
            primary_generation_request = {
                "prompt": prompt,
                "intent": resolved_intent,
                "model_role": attempted_profile.role,
                "output": dict(output),
                "max_tokens": (
                    128
                    if resolved_intent == "self_upgrade"
                    else inference_payload.get("max_output_tokens")
                ),
                "temperature": inference_payload.get("temperature"),
                "top_k": inference_payload.get("top_k"),
                "reasoning_effort": inference_payload.get("reasoning_effort"),
                "task_intensity": task_intensity,
                "requested_model": direct_runtime_model or automatic_runtime_model or None,
                "images": visual_inputs,
                "complex_pipeline": (
                    not direct_runtime_model
                    and str(
                        inference_payload.get("reasoning_effort") or "medium"
                    ).strip().casefold() != "none"
                    and (
                        task_intensity == "difficult"
                        if intensity_controlled
                        else (
                            (
                                inference_payload.get("autonomous_agent") is not False
                                and len(planned_intents) >= 1
                            )
                            or resolved_intent
                            in {"capabilities", "data_organization"}
                            or any(
                                marker in prompt.casefold()
                                for marker in (
                                    "複雜",
                                    "多步驟",
                                    "多階段",
                                    "complex task",
                                    "multi-step",
                                    "multistep",
                                )
                            )
                        )
                    )
                ),
                "reasoning_pipeline": (
                    not direct_runtime_model
                    and str(
                        inference_payload.get("reasoning_effort") or "medium"
                    ).strip().casefold() in {"medium", "high"}
                    and (
                        task_intensity in {"intermediate", "difficult"}
                        if intensity_controlled
                        else True
                    )
                    and resolved_intent
                    in {"reasoning", "calculation", "statistics", "analysis", "risk"}
                ),
                "division_pipeline": (
                    not direct_runtime_model
                    and str(
                        inference_payload.get("reasoning_effort") or "medium"
                    ).strip().casefold() != "none"
                    and (
                        task_intensity in {"normal", "intermediate"}
                        if intensity_controlled
                        else True
                    )
                    and resolved_intent
                    in self.transformer_runtime.DIVISION_OF_LABOR_INTENTS
                ),
                "cancel_event": inference_payload.get("_cancel_event"),
                "progress_callback": inference_payload.get("_progress_callback"),
            }
            collaboration_limit = {
                "simple": 1,
                "normal": 2,
                "intermediate": 3,
                "difficult": 4,
            }.get(task_intensity, 2)
            if resolved_intent in (
                self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
            ):
                collaboration_limit = 1
            auxiliary_specs: list[dict[str, str]] = []
            if not direct_runtime_model and collaboration_limit > 1:
                primary_candidates = self.transformer_runtime.model_candidates_for_intent(
                    resolved_intent,
                    str(inference_payload.get("reasoning_effort") or "medium"),
                    task_intensity,
                )
                used_models = set(primary_candidates[:1])
                secondary_intents = [
                    str(item)
                    for item in planned_intents[1:]
                    if str(item).strip()
                ] or [resolved_intent]
                while len(auxiliary_specs) < collaboration_limit - 1:
                    branch_intent = secondary_intents[
                        len(auxiliary_specs) % len(secondary_intents)
                    ]
                    candidates = self.transformer_runtime.model_candidates_for_intent(
                        branch_intent,
                        str(inference_payload.get("reasoning_effort") or "medium"),
                        task_intensity,
                    )
                    if task_intensity == "normal":
                        small_models = set(
                            self.transformer_runtime.MODEL_SIZE_TIERS["small"]
                        )
                        installed_small_models = [
                            str(item.get("name") or "")
                            for item in self.transformer_runtime.selectable_models(
                                refresh=False
                            )
                            if str(item.get("name") or "") in small_models
                        ]
                        candidates = [
                            *installed_small_models,
                            *[model for model in candidates if model not in small_models],
                        ]
                    branch_model = next(
                        (model for model in candidates if model not in used_models),
                        "",
                    )
                    if not branch_model:
                        break
                    used_models.add(branch_model)
                    auxiliary_specs.append(
                        {"intent": branch_intent, "model": branch_model}
                    )

            generation_calls = [
                asyncio.to_thread(
                    self.transformer_runtime.generate,
                    **primary_generation_request,
                )
            ]
            for spec in auxiliary_specs:
                generation_calls.append(
                    asyncio.to_thread(
                        self.transformer_runtime.generate,
                        prompt=prompt,
                        intent=spec["intent"],
                        model_role=f"parallel-specialist:{spec['intent']}",
                        output=dict(output),
                        max_tokens=inference_payload.get("max_output_tokens"),
                        temperature=inference_payload.get("temperature"),
                        top_k=inference_payload.get("top_k"),
                        reasoning_effort=inference_payload.get("reasoning_effort"),
                        task_intensity=task_intensity,
                        requested_model=spec["model"],
                        images=(
                            visual_inputs
                            if spec["intent"]
                            in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
                            else []
                        ),
                        complex_pipeline=False,
                        reasoning_pipeline=False,
                        division_pipeline=False,
                        cancel_event=inference_payload.get("_cancel_event"),
                        progress_callback=None,
                        _automatic_model_override=True,
                    )
                )
            generated_results = await asyncio.gather(*generation_calls)
            transformer_result = dict(generated_results[0])
            parallel_branches = [
                {
                    "sequence": index + 1,
                    "intent": spec["intent"],
                    "requested_model": spec["model"],
                    "selected_model": str(result.get("model") or spec["model"]),
                    "ok": result.get("ok") is True,
                    "error_code": str(result.get("error_code") or ""),
                    "text": str(result.get("text") or "")[:8_000],
                }
                for index, (spec, result) in enumerate(
                    zip(auxiliary_specs, generated_results[1:])
                )
            ]
            successful_parallel_branches = [
                branch for branch in parallel_branches if branch["ok"] is True
            ]
            integration_audit: dict[str, Any] = {
                "executed": False,
                "ok": transformer_result.get("ok") is True,
                "model": str(transformer_result.get("model") or ""),
            }
            if successful_parallel_branches and transformer_result.get("ok") is True:
                integration_model = (
                    self.DATA_COORDINATOR_MODEL
                    if task_intensity == "normal"
                    else self.FINAL_COORDINATOR_MODEL
                )
                installed_models = {
                    str(item.get("name") or "")
                    for item in self.transformer_runtime.selectable_models(
                        refresh=False
                    )
                }
                if integration_model in installed_models:
                    integration_context = dict(output)
                    integration_context["parallel_model_results"] = {
                        "primary": {
                            "intent": resolved_intent,
                            "model": str(transformer_result.get("model") or ""),
                            "text": str(transformer_result.get("text") or "")[:16_000],
                        },
                        "specialists": successful_parallel_branches,
                    }
                    integration_result = await asyncio.to_thread(
                        self.transformer_runtime.generate,
                        prompt=prompt,
                        intent="conversation",
                        model_role="parallel-results-integrator-and-verifier",
                        output=integration_context,
                        max_tokens=inference_payload.get("max_output_tokens"),
                        temperature=inference_payload.get("temperature"),
                        top_k=inference_payload.get("top_k"),
                        reasoning_effort=inference_payload.get("reasoning_effort"),
                        task_intensity=task_intensity,
                        requested_model=integration_model,
                        complex_pipeline=False,
                        reasoning_pipeline=False,
                        division_pipeline=False,
                        cancel_event=inference_payload.get("_cancel_event"),
                        progress_callback=inference_payload.get("_progress_callback"),
                        _automatic_model_override=True,
                    )
                    integration_audit = {
                        "executed": True,
                        "ok": integration_result.get("ok") is True,
                        "model": integration_model,
                        "error_code": str(integration_result.get("error_code") or ""),
                    }
                    if integration_result.get("ok") is True:
                        transformer_result = dict(integration_result)
            transformer_result["parallel_model_execution"] = {
                "enabled": bool(auxiliary_specs),
                "policy": "parallel-independent-subtasks-sequential-dependent-stages",
                "task_intensity": task_intensity,
                "maximum_parallel_branches": collaboration_limit,
                "actual_parallel_branches": 1 + len(auxiliary_specs),
                "failure_only_model_escalation": True,
                "primary": {
                    "intent": resolved_intent,
                    "ok": generated_results[0].get("ok") is True,
                    "model": str(generated_results[0].get("model") or ""),
                },
                "specialists": parallel_branches,
                "integration": integration_audit,
            }
            self._record_ollama_inference(
                transformer_result,
                intent=resolved_intent,
                model_role=attempted_profile.role,
                request={
                    "prompt": prompt,
                    "planned_intents": planned_intents,
                    "reasoning_effort": inference_payload.get("reasoning_effort"),
                    "task_intensity": task_intensity,
                    "generation_speed": inference_payload.get("generation_speed"),
                    "autonomous_agent": inference_payload.get("autonomous_agent")
                    is not False,
                },
            )
            transformer_latency = round(
                (time.perf_counter() - transformer_started) * 1_000, 3
            )
            self._runtime_metrics["transformer_last_latency_ms"] = transformer_latency
            self._runtime_metrics["transformer_latency_ms_total"] = round(
                float(self._runtime_metrics["transformer_latency_ms_total"])
                + transformer_latency,
                3,
            )
            output["transformer_inference"] = transformer_result
            if transformer_result.get("ok") is True:
                self._runtime_metrics["transformer_success_count"] = int(
                    self._runtime_metrics["transformer_success_count"]
                ) + 1
                self._infer_apply_transformer_success(
                    output,
                    transformer_result,
                    resolved_intent,
                    direct_runtime_model,
                    profile,
                )
            else:
                self._runtime_metrics["transformer_fallback_count"] = int(
                    self._runtime_metrics["transformer_fallback_count"]
                ) + 1
                if (
                    not native_model_requested
                    or resolved_intent
                    in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
                ):
                    self._runtime_metrics["error_count"] = int(
                        self._runtime_metrics["error_count"]
                    ) + 1
                    failed_model = (
                        direct_runtime_model
                        or self.transformer_runtime.preferred_model_for_intent(
                            resolved_intent
                        )
                    )
                    return "xingcheng_infer_result", {
                        "ok": False,
                        "error_code": str(
                            transformer_result.get("error_code")
                            or "TRANSFORMER_INFERENCE_FAILED"
                        ),
                        "message": (
                            f"本機 Ollama 模型 {failed_model} 無法完成推論："
                            f"{str(transformer_result.get('message') or '模型服務未就緒')}"
                        ),
                        "selected_runtime_model": failed_model,
                        "model_selection": (
                            "user-selected" if direct_runtime_model else "automatic"
                        ),
                        "manual_model_selection": bool(direct_runtime_model),
                        "fallback_model_used": False,
                        "star_native_model_used": False,
                        "external_ai_used": False,
                        "retryable": True,
                        "transformer_inference": transformer_result,
                    }
                generation = output.get("generation")
                if isinstance(generation, dict):
                    generation["transformer_fallback_used"] = True
                    generation["transformer_fallback_reason"] = str(
                        transformer_result.get("error_code")
                        or "TRANSFORMER_RUNTIME_UNAVAILABLE"
                    )
                if resolved_intent == "self_upgrade" and isinstance(
                    output.get("self_repair"), dict
                ):
                    repair = output["self_repair"]
                    completed = repair.get("status") == "completed"
                    summary = (
                        "已執行星澄自我檢討與維護；模型、資料與能力健康檢查已完成。"
                        if completed
                        else "已執行星澄自我檢討與維護；仍有項目需要後續處理。"
                    )
                    detail = str(output.get("response") or "").strip()
                    output["response"] = f"{summary}\n\n{detail}" if detail else summary
                    if isinstance(generation, dict):
                        generation["text"] = output["response"]
        return None


    async def _infer_prepare_intents(
        self,
        payload: dict[str, Any],
        command_plan: dict[str, Any],
        raw_command: str,
    ) -> tuple[dict[str, Any], str, list[str], str, list[dict[str, Any]]]:
        inference_payload = dict(payload)
        inference_payload["_semantic_plan"] = command_plan
        assessed_intensity = str(
            (command_plan.get("task_intensity") or {}).get("level") or "normal"
        ).strip().casefold()
        requested_intensity = str(
            inference_payload.get("requested_task_intensity")
            or inference_payload.get("task_intensity")
            or ""
        ).strip().casefold()
        automatic_intensity = (
            requested_intensity
            if requested_intensity in {"simple", "normal", "intermediate", "difficult"}
            else assessed_intensity
        )
        if automatic_intensity not in {
            "simple",
            "normal",
            "intermediate",
            "difficult",
        }:
            automatic_intensity = "normal"
        if payload.get("_task_intensity_override_authorized") is not True:
            inference_payload["task_intensity"] = automatic_intensity
            inference_payload["task_intensity_source"] = (
                "automatic-traditional-chinese-command-assessment"
            )
            inference_payload["max_output_tokens"] = {
                "simple": 256,
                "normal": 512,
                "intermediate": 768,
                "difficult": 1_024,
            }[automatic_intensity]
        self._runtime_metrics["inference_request_count"] = int(
            self._runtime_metrics["inference_request_count"]
        ) + 1
        self._runtime_metrics["last_activity_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        prompt = str(
            inference_payload.get("instruction")
            or inference_payload.get("prompt")
            or ""
        )
        planned_intents = list(command_plan.get("intents") or [])
        if not planned_intents:
            planned_intents = ["conversation"]
        if self.reading_expert.has_readable_content(inference_payload):
            planned_intents = [
                "reading",
                *(intent for intent in planned_intents if intent != "reading"),
            ]
        external_tasks: list[dict[str, Any]] = []
        planned_intent = planned_intents[0]
        await asyncio.to_thread(
            self._repository_for(self.models.MAIN).record_common_command,
            raw_command,
            planned_intent,
        )
        return (
            inference_payload,
            prompt,
            planned_intents,
            planned_intent,
            external_tasks,
        )


    async def _infer_validate_request(
        self,
        payload: dict[str, Any],
    ) -> tuple[tuple[str, dict[str, Any]] | None, str, bool, str]:
        capability_request = payload.get("capability_composition")
        if isinstance(capability_request, dict):
            if (
                str(payload.get("runtime_model") or "") != self.NATIVE_MODEL_ID
                or payload.get("_native_internal_operation") is not True
            ):
                return ("xingcheng_infer_result", {
                    "ok": False,
                    "error_code": "NATIVE_INTERNAL_OPERATION_REQUIRED",
                    "message": "能力編成只由星澄原生模型內部處理。",
                }), "", False, ""
            result = await self._compose_capability_with_vote(capability_request)
            result["internal_owner"] = self.NATIVE_MODEL_ID
            return ("xingcheng_infer_result", result), "", False, ""
        manual_model_fields = (
            "model",
            "model_id",
            "assigned_model",
            "specialist",
        )
        if any(str(payload.get(key) or "").strip() for key in manual_model_fields):
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "DIRECT_MODEL_ACCESS_DENIED",
                "message": "模型由星澄主模型自動安排，不允許直接指定。",
                "model_selection": "automatic",
            }), "", False, ""
        requested_runtime_model = str(payload.get("runtime_model") or "").strip()
        native_model_requested = requested_runtime_model == self.NATIVE_MODEL_ID
        if native_model_requested:
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "STAR_NATIVE_TASK_PARTICIPATION_DENIED",
                "message": "星澄不參與任務；請使用已分配職責的 Ollama 本地模型。",
                "star_native_model_used": False,
            }), "", False, ""
        direct_runtime_model = (
            "" if native_model_requested else requested_runtime_model
        )
        if (
            requested_runtime_model
            and payload.get("_runtime_model_selection_authorized") is not True
        ):
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "RUNTIME_MODEL_SELECTION_DENIED",
                "message": "只有受治理的模型對話工具可以選擇本機生成模型。",
            }), "", False, ""
        if requested_runtime_model:
            selectable_names = {
                str(item.get("name") or "")
                for item in self.transformer_runtime.selectable_models(refresh=False)
            }
            if requested_runtime_model not in selectable_names:
                return ("xingcheng_infer_result", {
                    "ok": False,
                    "error_code": "RUNTIME_MODEL_NOT_INSTALLED",
                    "message": "選擇的模型未安裝或不符合本機模型名稱規則。",
                    "selectable_models": sorted(selectable_names),
                }), "", False, ""
        if requested_runtime_model and not self.transformer_runtime.enabled:
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "LOCAL_MODEL_RUNTIME_REQUIRED",
                "message": "本地模型執行環境未啟用；任務不會回退交給星澄。",
                "star_native_model_used": False,
                "fallback_model_used": False,
            }), "", False, ""
        return None, requested_runtime_model, native_model_requested, direct_runtime_model


    def _infer_build_command_plan(
        self,
        payload: dict[str, Any],
    ) -> tuple[
        tuple[str, dict[str, Any]] | None,
        str,
        str,
        dict[str, Any],
        Any,
        str,
    ]:
        prompt = str(payload.get("instruction") or payload.get("prompt") or "")
        raw_command = str(payload.get("user_command") or prompt).strip()
        requested_programming_folder = self._programming_folder_for_request(payload)
        if not requested_programming_folder:
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_REQUIRED",
                "message": "請先選擇編程資料夾。",
            }), "", "", {}, None, ""
        try:
            programming_folder_path = Path(requested_programming_folder).resolve()
        except OSError:
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_INVALID",
                "message": "選擇的編程資料夾路徑無效。",
            }), "", "", {}, None, ""
        if not programming_folder_path.is_dir():
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_NOT_FOUND",
                "message": "選擇的編程資料夾不存在或不是資料夾。",
            }), "", "", {}, None, ""
        programming_folder = str(programming_folder_path)
        payload["programming_folder"] = programming_folder
        command_context = str(payload.get("_command_context") or "").strip()
        textual_confirmation = bool(
            re.match(
                r"^\s*(?:我(?:已)?|本人)?\s*確認(?:執行|刪除|操作|繼續)",
                raw_command,
                flags=re.IGNORECASE,
            )
        )
        explicit_confirmation = bool(
            payload.get("confirmed") is True
            or payload.get("destructive_confirmation") is True
            or textual_confirmation
        )
        command_understanding_model = "deterministic-zh-tw-fast-path"
        progress_callback = payload.get("_progress_callback")
        if callable(progress_callback):
            progress_callback({
                "phase": "command-understanding",
                "model": command_understanding_model,
                "message": "正在直接理解繁體中文並建立任務計畫",
            })
        command_plan = self._repository_for(self.models.MAIN).command_parser.parse(
            raw_command,
            self.native_model.semantic_plan(
                raw_command,
                context=command_context,
                confirmed=explicit_confirmation,
            ),
        )
        command_plan["programming_scope"] = {
            "project_root": programming_folder,
            "selected_folder": programming_folder,
            "outside_project_access": False,
        }
        command_plan["command_understanding"] = {
            "recognized": True,
            "model": "deterministic-zh-tw-fast-path",
            "star_native_model_used": False,
        }
        return None, prompt, raw_command, command_plan, progress_callback, command_understanding_model


    async def _infer_run_command_preflight_gates(
        self,
        payload: dict[str, Any],
        command_plan: dict[str, Any],
        raw_command: str,
        prompt: str,
        native_model_requested: bool,
        progress_callback: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        run_frontend_worker = (
            str(payload.get("task_intensity") or "normal").strip().casefold()
            == "difficult"
            and
            str(payload.get("generation_speed") or "medium").strip().casefold()
            not in {"high", "ultra"}
            and (
                str(payload.get("conversation_mode") or "").strip().casefold() == "coding"
                or any(
                    intent in {"coding", "calculation", "statistics", "reasoning", "command_execution"}
                    for intent in command_plan.get("intents") or []
                )
            )
        )
        if callable(progress_callback) and run_frontend_worker:
            progress_callback({
                "phase": "workflow-frontend",
                "model": self.transformer_runtime.FRONTEND_WORKER_MODEL,
                "message": "正在整理執行流程與模型路由",
            })
        if run_frontend_worker:
            frontend_result = await asyncio.to_thread(
                self._run_rnj_frontend_worker,
                raw_command,
                command_plan,
            )
            command_plan["frontend_worker"] = {
                "ok": frontend_result.get("ok") is True,
                "model": str(
                    frontend_result.get("model")
                    or self.transformer_runtime.FRONTEND_WORKER_MODEL
                ),
                "text": str(frontend_result.get("text") or "")[:8_000],
                "position": "after-command-understanding-for-code-and-stem",
                "optional": True,
                "dynamic_reassignment": dict(
                    frontend_result.get("dynamic_model_reassignment") or {}
                ),
            }
        safety = command_plan.get("safety")
        if isinstance(safety, dict) and safety.get("confirmation_required") is True:
            return "xingcheng_infer_result", {
                "ok": False,
                "error_code": "SAFE_CONFIRMATION_REQUIRED",
                "message": (
                    "命令涉及可能的破壞性操作，且利用目前上下文補全後仍有歧義。"
                    "請明確指出操作對象與範圍，並確認是否執行；目前未執行任何操作。"
                ),
                "status": "confirmation-required",
                "operation_executed": False,
                "semantic_understanding": command_plan,
                "remaining_ambiguities": list(
                    safety.get("remaining_ambiguities") or []
                ),
                "safety_gate": safety,
            }
        if native_model_requested and any(
            token in prompt.casefold()
            for token in (
                "用ollama訓練星澄",
                "用 ollama 訓練星澄",
                "ollama訓練星澄",
                "ollama 訓練星澄",
                "本地模型訓練星澄",
                "本機模型訓練星澄",
                "train star with ollama",
            )
        ):
            result = await self._train_with_ollama(payload)
            result["intent"] = "ollama_native_model_training"
            self._identify_model(result, self.models.primary)
            return "xingcheng_infer_result", result
        if (
            not native_model_requested
            and "參數" in prompt
            and any(token in prompt for token in ("調整", "修改", "優化", "校準"))
        ):
            result = await self._tune_investment_parameters(payload)
            result["intent"] = "investment_parameter_tuning"
            self._identify_model(result, self.models.primary)
            return "xingcheng_infer_result", result
        return None


    def _infer_plan_session_context(
        self,
        inference_payload: dict[str, Any],
        planned_intent: str,
        prompt: str,
        direct_runtime_model: str,
        native_model_requested: bool,
        command: str,
    ) -> tuple[
        tuple[str, dict[str, Any]] | None,
        str,
        Any,
        str,
        str,
    ]:
        automatic_runtime_model = (
            ""
            if direct_runtime_model
            else self.transformer_runtime.select_model_for_request(
                planned_intent,
                reasoning_effort=str(inference_payload.get("reasoning_effort") or "medium"),
                task_intensity=str(inference_payload.get("task_intensity") or "normal"),
                generation_speed=str(inference_payload.get("generation_speed") or "medium"),
            )
        )
        if (
            planned_intent
            in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
            and not self.transformer_runtime.enabled
        ):
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "VISUAL_SPECIALIST_UNAVAILABLE",
                "message": (
                    "視覺檔案辨識固定使用 MiniCPM-V 4.6；目前本機 Transformer "
                    "執行環境未啟用，因此未改派其他模型，也未猜測視覺內容。"
                ),
                "intent": planned_intent,
                "required_model": (
                    self.transformer_runtime.VISUAL_FILE_MANAGEMENT_MODEL
                ),
            }), "", "", "", ""
        if planned_intent in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS:
            visual_model = self.transformer_runtime.VISUAL_FILE_MANAGEMENT_MODEL
            installed_visual_models = {
                str(item.get("name") or "")
                for item in self.transformer_runtime.selectable_models(
                    refresh=True
                )
            }
            if visual_model not in installed_visual_models:
                return ("xingcheng_infer_result", {
                    "ok": False,
                    "error_code": "VISUAL_SPECIALIST_NOT_INSTALLED",
                    "message": (
                        "視覺檔案辨識固定使用 MiniCPM-V 4.6，但模型 "
                        f"{visual_model} 尚未安裝；目前未改派其他模型。"
                    ),
                    "intent": planned_intent,
                    "required_model": visual_model,
                }), "", "", "", ""
        inference_payload["_governed_intent"] = planned_intent
        planned_profile = (
            self.models.MAIN
            if native_model_requested
            else self.models.for_command(command, intent=planned_intent)
        )
        business_scope = (
            "investment"
            if not native_model_requested
            and (
                planned_profile == self.models.INVESTMENT
                or planned_intent in {"search", "analysis", "risk"}
            )
            else "general"
        )
        inference_payload["memory_context"] = (
            self.memory_broker.context_for_inference(
                business_scope,
                planned_intent,
                prompt,
                owner_only=True,
            )
            if native_model_requested
            else []
        )
        if native_model_requested:
            inference_payload["native_private_context"] = self._repository_for(
                self.models.MAIN
            ).native_private_context()
        if planned_intent in {"analysis", "risk"} and not native_model_requested:
            governed_parameters = self.investment_repository.investment_parameter_values()
            requested_parameters = inference_payload.get("analysis_parameters")
            if not isinstance(requested_parameters, dict):
                requested_parameters = {}
            inference_payload["analysis_parameters"] = {
                "position_concentration_percent": governed_parameters[
                    "max_single_position_percent"
                ],
                "missing_data_warning_percent": governed_parameters[
                    "missing_data_warning_percent"
                ],
                **requested_parameters,
            }
        if planned_profile.network_policy == "disabled":
            inference_payload["allow_network"] = False
        assigned_model = (
            self.NATIVE_MODEL_ID
            if native_model_requested
            else direct_runtime_model
            or automatic_runtime_model
            or self._ollama_model_for_intent(
                planned_intent,
                str(inference_payload.get("task_intensity") or "normal"),
            )
        )
        progress_callback = inference_payload.get("_progress_callback")
        if callable(progress_callback):
            progress_callback(
                {
                    "phase": "command-planned",
                    "model": assigned_model,
                    "intent": planned_intent,
                }
            )
        return None, automatic_runtime_model, planned_profile, business_scope, assigned_model


    async def _infer_generate_model_output(
        self,
        inference_payload: dict[str, Any],
        prompt: str,
        planned_intent: str,
        planned_profile: Any,
        native_model_requested: bool,
    ) -> tuple[tuple[str, dict[str, Any]] | None, dict[str, Any]]:
        inference_started = time.perf_counter()
        output = (
            await asyncio.to_thread(
                self.model_engines.for_profile(planned_profile).infer,
                inference_payload,
                database=self._repository_for(planned_profile).database_status(),
                analyze=analyze_investments,
                search=self.market_data.search,
            )
            if native_model_requested or not self.transformer_runtime.enabled
            else await asyncio.to_thread(
                self._prepare_ollama_output,
                inference_payload,
                prompt,
                planned_intent,
            )
        )
        self._record_latency("inference", inference_started)
        if not output.get("ok"):
            self._runtime_metrics["error_count"] = int(
                self._runtime_metrics["error_count"]
            ) + 1
            return ("xingcheng_infer_result", output), None
        return None, output
