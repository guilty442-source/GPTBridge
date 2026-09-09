from __future__ import annotations

from typing import Any


class InferHandleMixin:

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
