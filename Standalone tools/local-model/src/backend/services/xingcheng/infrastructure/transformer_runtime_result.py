from __future__ import annotations

import time
from typing import Any, Mapping

from .transformer_runtime_support import _GeneratePlan


class TransformerRuntimeResultMixin:
    """Success-result assembly and governed retry/reassignment policy."""

    def _generate_success_result(
        self,
        plan: _GeneratePlan,
        prepared: Mapping[str, Any],
        *,
        text: str,
        response: Mapping[str, Any],
        request_payload: Mapping[str, Any],
        context_state: Mapping[str, int],
        context_attempts: list[dict[str, Any]],
        unsupported: Mapping[str, list[str]],
        started: float,
        resource_allocation: Any,
    ) -> dict[str, Any]:
        return {
            **self._result_identity_fields(plan, text, request_payload),
            "adaptive_context": self._adaptive_context_report(
                prepared=prepared,
                request_payload=request_payload,
                context_state=context_state,
                context_attempts=context_attempts,
            ),
            "prompt_eval_count": int(response.get("prompt_eval_count") or 0),
            "eval_count": int(response.get("eval_count") or 0),
            "load_duration_ns": int(response.get("load_duration") or 0),
            "total_duration_ns": int(response.get("total_duration") or 0),
            "latency_ms": round((time.perf_counter() - started) * 1_000, 3),
            "facts_supported": not unsupported,
            "unsupported_facts": unsupported,
            "remote_network_used": False,
            "loopback_runtime_used": True,
            "third_party_foundation_weights": True,
            "foundation_model_license": plan.selected_metadata["license"],
            "model_selected_by_user": plan.user_selected_model,
            "resource_allocation": resource_allocation,
            "reasoning_effort": plan.normalized_reasoning_effort,
            "task_intensity": plan.normalized_task_intensity,
            "residency": plan.selected_metadata["residency"],
            "parameter_profile": self._parameter_profile_report(
                plan, prepared
            ),
        }

    @staticmethod
    def _result_identity_fields(
        plan: _GeneratePlan,
        text: str,
        request_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "text": text,
            "decoder": "quantized-transformer-autoregressive-decoder",
            "model": plan.selected_model,
            "model_family": plan.selected_metadata["family"],
            "parameter_class": plan.selected_metadata["parameter_class"],
            "parameter_count": plan.selected_metadata["parameter_count"],
            "quantization": plan.selected_metadata["quantization"],
            "architecture": plan.selected_metadata["architecture"],
            "context_window": int(request_payload["options"]["num_ctx"]),
        }

    def _adaptive_context_report(
        self,
        *,
        prepared: Mapping[str, Any],
        request_payload: Mapping[str, Any],
        context_state: Mapping[str, int],
        context_attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "automatic": True,
            "safe_start": self.SAFE_CONTEXT_WINDOW,
            "required_context": prepared["required_context"],
            "initial_context": prepared["selected_context"],
            "used_context": int(request_payload["options"]["num_ctx"]),
            "next_context_ceiling": int(
                context_state.get("context_ceiling")
                or prepared["selected_context"]
            ),
            "memory_pressure_recovered": len(context_attempts) > 1,
            "attempts": context_attempts,
            "content_preserved": True,
        }

    def _parameter_profile_report(
        self, plan: _GeneratePlan, prepared: Mapping[str, Any]
    ) -> dict[str, Any]:
        parameter_settings = plan.parameter_settings
        return {
            "source": (
                "immutable-base-defaults"
                if plan.use_immutable_base
                else "immutable-base-plus-dynamic-overrides"
            ),
            "selected_mode": str(parameter_settings["selected_mode"]),
            "dynamic_overrides_enabled": bool(
                parameter_settings["dynamic_overrides_enabled"]
            ),
            "role": str(parameter_settings["role"]),
            "context_limit": int(parameter_settings["context_limit"]),
            "default_output_tokens": int(
                parameter_settings["default_output_tokens"]
            ),
            "max_output_tokens": int(
                parameter_settings["max_output_tokens"]
            ),
            "thinking": str(parameter_settings["thinking"]),
            "keep_alive": prepared["configured_keep_alive"],
            "generation_options": dict(prepared["configured_generation"]),
        }

    def _automatic_route_report(self, plan: _GeneratePlan) -> dict[str, Any]:
        return {
            "intent": str(plan.intent),
            "reasoning_effort": plan.normalized_reasoning_effort,
            "primary_model": plan.selected_model,
            "selected_model": plan.selected_model,
            "fallback_used": False,
            "escalation_policy": "failure-only",
            "escalation_trigger": "",
            "attempts": [{"model": plan.selected_model, "ok": True, "error_code": ""}],
        }

    def _generate_failure_payload(
        self,
        plan: _GeneratePlan,
        prepared: Mapping[str, Any],
        *,
        context_attempts: list[dict[str, Any]],
        error: BaseException,
        started: float,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": (
                "TRANSFORMER_MEMORY_PRESSURE"
                if self._is_memory_pressure(error)
                else "TRANSFORMER_INFERENCE_FAILED"
            ),
            "message": str(error)[:500],
            "fallback_required": True,
            "latency_ms": round((time.perf_counter() - started) * 1_000, 3),
            "adaptive_context": {
                "automatic": True,
                "required_context": prepared["required_context"],
                "initial_context": prepared["selected_context"],
                "attempts": context_attempts,
                "content_preserved": True,
                "resumable": True,
            },
        }

    def _retry_automatic_route(
        self, plan: _GeneratePlan, failure: dict[str, Any]
    ) -> dict[str, Any]:
        failure, recovered = self._retry_immutable_base(plan, failure)
        if recovered is not None:
            return recovered
        commander_adjudication = (
            {
                "adjudicator_model": self.FAILURE_ADJUDICATOR_MODEL,
                "decision": "stop-after-maximum-dynamic-reassignments",
                "assigned_model": "",
                "dynamic_reassignment": False,
            }
            if plan.automatic_model_override
            else self._commander_adjudicate_model_failure(
                failed_model=plan.selected_model,
                failure=failure,
                intent=plan.intent,
                task_intensity=plan.normalized_task_intensity,
                model_catalog=plan.model_catalog,
            )
        )
        failure, reassigned = self._retry_commander_reassign(
            plan, failure, commander_adjudication
        )
        if reassigned is not None:
            return reassigned
        return self._retry_failure_result(plan, failure, commander_adjudication)

    def _retry_immutable_base(
        self, plan: _GeneratePlan, failure: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        advisor_policy = self.parameter_policy.temporary_parameter_advisor()
        if (
            plan.base_default_retry
            or plan.automatic_model_override
            or advisor_policy.get("enabled") is not True
            or str(advisor_policy.get("decision") or "")
            != "restore-immutable-base-defaults"
            or (plan.cancel_event is not None and plan.cancel_event.is_set())
        ):
            return failure, None
        base_retry = self.generate(
            prompt=plan.prompt,
            intent=plan.intent,
            model_role=plan.model_role,
            output=plan.output,
            max_tokens=None,
            temperature=None,
            top_k=None,
            reasoning_effort=None,
            task_intensity=plan.normalized_task_intensity,
            requested_model=plan.selected_model,
            images=plan.visual_inputs,
            complex_pipeline=False,
            reasoning_pipeline=False,
            division_pipeline=False,
            cancel_event=plan.cancel_event,
            progress_callback=plan.progress_callback,
            _release_after_generate=plan.release_after_generate,
            _automatic_model_override=True,
            _use_immutable_base=True,
            _base_default_retry=True,
        )
        adjudication = {
            "advisor_model": self.FAILURE_ADJUDICATOR_MODEL,
            "decision": "restore-immutable-base-defaults",
            "persisted": False,
            "attempted": True,
            "ok": base_retry.get("ok") is True,
            "trigger_error_code": str(failure.get("error_code") or ""),
        }
        if base_retry.get("ok") is True:
            recovered = dict(base_retry)
            recovered["temporary_parameter_adjudication"] = adjudication
            return failure, recovered
        failure = dict(failure)
        failure["temporary_parameter_adjudication"] = adjudication
        return failure, None

    def _retry_commander_reassign(
        self,
        plan: _GeneratePlan,
        failure: dict[str, Any],
        commander_adjudication: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        assigned_model = str(
            commander_adjudication.get("assigned_model") or ""
        )
        if (
            not assigned_model
            or plan.automatic_model_override
            or (plan.cancel_event is not None and plan.cancel_event.is_set())
        ):
            return failure, None
        reassigned_result = self.generate(
            prompt=plan.prompt,
            intent=plan.intent,
            model_role=f"commander-reassigned-after:{plan.selected_model}",
            output=plan.output,
            max_tokens=plan.max_tokens,
            temperature=plan.temperature,
            top_k=plan.top_k,
            reasoning_effort=plan.normalized_reasoning_effort,
            task_intensity=plan.normalized_task_intensity,
            requested_model=assigned_model,
            images=plan.visual_inputs,
            complex_pipeline=False,
            reasoning_pipeline=False,
            division_pipeline=False,
            cancel_event=plan.cancel_event,
            progress_callback=plan.progress_callback,
            _release_after_generate=plan.release_after_generate,
            _automatic_model_override=True,
        )
        reassigned_result["failure_adjudication"] = commander_adjudication
        reassigned_result["dynamic_model_reassignment"] = {
            "failed_owner_model": plan.selected_model,
            "assigned_model": assigned_model,
            "assigned_by": self.FAILURE_ADJUDICATOR_MODEL,
            "maximum_reassignments": 1,
            "preconfigured_backup_used": False,
        }
        if reassigned_result.get("ok") is True:
            return failure, reassigned_result
        return reassigned_result, None

    @staticmethod
    def _retry_failure_result(
        plan: _GeneratePlan,
        failure: dict[str, Any],
        commander_adjudication: dict[str, Any],
    ) -> dict[str, Any]:
        fixed_owner_failure = dict(failure)
        fixed_owner_failure["automatic_model_route"] = {
            "intent": str(plan.intent),
            "reasoning_effort": plan.normalized_reasoning_effort,
            "fixed_owner_model": plan.selected_model,
            "fallback_used": False,
            "backup_policy": "none",
            "attempts": [
                {
                    "model": plan.selected_model,
                    "ok": False,
                    "error_code": str(
                        failure.get("error_code")
                        or "TRANSFORMER_INFERENCE_FAILED"
                    ),
                }
            ],
        }
        fixed_owner_failure["failure_adjudication"] = commander_adjudication
        return fixed_owner_failure
