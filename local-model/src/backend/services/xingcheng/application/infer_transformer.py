from __future__ import annotations

import asyncio
import time
from typing import Any


class InferTransformerMixin:

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
