from __future__ import annotations

import time
import urllib.error
from contextlib import nullcontext
from typing import Any, Callable, Mapping

from .transformer_runtime_support import (
    _GeneratePlan,
    _generate_request_args,
    _resource_preparation_lock,
)


def _generate_cancelled() -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "TRANSFORMER_REQUEST_CANCELLED",
        "message": "Model generation was cancelled",
        "fallback_required": False,
    }


def _generate_unavailable(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "TRANSFORMER_RUNTIME_UNAVAILABLE",
        "message": str(status.get("last_error") or "runtime unavailable"),
        "fallback_required": True,
    }


def _generate_not_installed(model_catalog: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "TRANSFORMER_MODEL_NOT_INSTALLED",
        "message": "selected local model is not installed",
        "fallback_required": False,
        "selectable_models": list(model_catalog.values()),
    }


class TransformerRuntimeInferenceMixin:
    """Governed generate() orchestration and single-model inference path."""

    @_resource_preparation_lock
    def generate(
        self, *, prompt: str, intent: str, model_role: str,
        output: Mapping[str, Any], max_tokens: Any = None,
        temperature: Any = None, top_k: Any = None,
        reasoning_effort: Any = None, task_intensity: Any = None,
        requested_model: Any = None, images: Any = None,
        complex_pipeline: bool = False, reasoning_pipeline: bool = False,
        division_pipeline: bool = False, cancel_event: Any = None,
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
        response_format: str | Mapping[str, Any] | None = None,
        _release_after_generate: bool = False,
        _automatic_model_override: bool = False,
        _use_immutable_base: bool = False, _base_default_retry: bool = False,
        _user_designated_model: bool = False,
        _dialogue_interactive: bool = False,
    ) -> dict[str, Any]:
        request = _generate_request_args(
            prompt, intent, model_role, output, max_tokens, temperature,
            top_k, reasoning_effort, task_intensity, requested_model, images,
            complex_pipeline, reasoning_pipeline, division_pipeline,
            cancel_event, progress_callback, response_format,
            _release_after_generate, _automatic_model_override,
            _use_immutable_base, _base_default_retry,
            _user_designated_model, _dialogue_interactive,
        )
        denied = self._generate_precheck(request)
        if denied is not None:
            return denied
        route = self._generate_route(request)
        denied = self._generate_model_denial(request, route)
        if denied is not None:
            return denied
        model_catalog = self._generate_model_catalog()
        if self._generate_wants_pipeline(request):
            return self._generate_pipeline(
                request, route=route, model_catalog=model_catalog
            )
        selected_metadata = model_catalog.get(route["selected_model"])
        if selected_metadata is None:
            return _generate_not_installed(model_catalog)
        plan = self._new_generate_plan(
            request,
            route=route,
            model_catalog=model_catalog,
            selected_metadata=selected_metadata,
        )
        return self._generate_direct(plan)

    def _generate_precheck(
        self, request: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        cancel_event = request["cancel_event"]
        if cancel_event is not None and cancel_event.is_set():
            return _generate_cancelled()
        status = self.probe(
            refresh=not bool(self._status.get("last_probed_at"))
        )
        if not status.get("available"):
            return _generate_unavailable(status)
        return None

    def _generate_route(
        self, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        normalized_task_intensity = (
            str(request["task_intensity"] or "").strip().casefold()
        )
        if normalized_task_intensity not in {
            "simple",
            "normal",
            "intermediate",
            "difficult",
        }:
            normalized_task_intensity = ""
        routing_parameters = self._cached_resolve(
            model="",
            task_intensity=normalized_task_intensity or "normal",
            reasoning_effort=request["reasoning_effort"],
            request_key=request["prompt"],
            immutable_base=request["use_immutable_base"],
        )
        normalized_reasoning_effort = str(
            routing_parameters["reasoning_effort"]
        )
        selected_model = self._resolve_selected_model(
            request, normalized_reasoning_effort, normalized_task_intensity
        )
        parameter_settings = self._cached_resolve(
            model=selected_model,
            task_intensity=normalized_task_intensity or "normal",
            reasoning_effort=normalized_reasoning_effort,
            request_key=request["prompt"],
            immutable_base=request["use_immutable_base"],
        )
        return {
            "normalized_task_intensity": normalized_task_intensity,
            "normalized_reasoning_effort": normalized_reasoning_effort,
            "visual_inputs": self._bounded_visual_inputs(request["images"]),
            "user_selected_model": bool(request["requested_model"])
            and not request["automatic_model_override"],
            "user_designated_model": bool(request["user_designated_model"]),
            "dialogue_interactive": bool(request["dialogue_interactive"]),
            "selected_model": selected_model,
            "parameter_settings": parameter_settings,
        }

    def _resolve_selected_model(
        self,
        request: Mapping[str, Any],
        normalized_reasoning_effort: str,
        normalized_task_intensity: str,
    ) -> str:
        route_candidates = (
            []
            if request["requested_model"]
            else self.model_candidates_for_intent(
                request["intent"],
                normalized_reasoning_effort,
                normalized_task_intensity,
                refresh=False,
            )
        )
        return str(
            request["requested_model"]
            or (
                self.MODEL
                if normalized_reasoning_effort == "none"
                and str(request["intent"] or "").strip().casefold()
                not in self.VISUAL_FILE_MANAGEMENT_INTENTS
                else route_candidates[0]
            )
        ).strip()

    def _generate_model_denial(
        self, request: Mapping[str, Any], route: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        selected_model = route["selected_model"]
        if (
            selected_model == self.VISUAL_FILE_MANAGEMENT_MODEL
            and str(request["intent"] or "").strip().casefold()
            not in (*self.VISUAL_FILE_MANAGEMENT_INTENTS, "command_understanding")
        ):
            return {
                "ok": False,
                "error_code": "VISUAL_SPECIALIST_SCOPE_DENIED",
                "message": (
                    "MiniCPM-V 4.6 僅允許處理視覺檔案辨識、分類、標籤與摘要。"
                ),
                "fallback_required": False,
            }
        if self.MODEL_NAME_PATTERN.fullmatch(selected_model) is None:
            return {
                "ok": False,
                "error_code": "TRANSFORMER_MODEL_SELECTION_INVALID",
                "message": "selected model name is invalid",
                "fallback_required": False,
            }
        return None

    def _generate_model_catalog(self) -> dict[str, Any]:
        return {
            str(item.get("name")): item
            for item in self.selectable_models(refresh=False)
        }

    @staticmethod
    def _generate_wants_pipeline(request: Mapping[str, Any]) -> bool:
        return (
            (
                request["complex_pipeline"]
                or request["reasoning_pipeline"]
                or request["division_pipeline"]
            )
            and not request["requested_model"]
        )

    def _new_generate_plan(
        self,
        request: Mapping[str, Any],
        *,
        route: Mapping[str, Any],
        model_catalog: Mapping[str, Any],
        selected_metadata: Mapping[str, Any],
    ) -> _GeneratePlan:
        return _GeneratePlan(
            prompt=request["prompt"],
            intent=request["intent"],
            model_role=request["model_role"],
            output=request["output"],
            max_tokens=request["max_tokens"],
            temperature=request["temperature"],
            top_k=request["top_k"],
            reasoning_effort=request["reasoning_effort"],
            requested_model=request["requested_model"],
            response_format=request["response_format"],
            cancel_event=request["cancel_event"],
            progress_callback=request["progress_callback"],
            release_after_generate=request["release_after_generate"],
            automatic_model_override=request["automatic_model_override"],
            use_immutable_base=request["use_immutable_base"],
            base_default_retry=request["base_default_retry"],
            normalized_task_intensity=route["normalized_task_intensity"],
            normalized_reasoning_effort=route["normalized_reasoning_effort"],
            selected_model=route["selected_model"],
            user_selected_model=route["user_selected_model"],
            user_designated_model=route["user_designated_model"],
            dialogue_interactive=route["dialogue_interactive"],
            visual_inputs=route["visual_inputs"],
            model_catalog=model_catalog,
            normalized_prompt=str(request["prompt"] or "").strip(),
            context=self._structured_context(request["output"]),
            selected_metadata=selected_metadata,
            parameter_settings=route["parameter_settings"],
        )

    def _generate_direct(self, plan: _GeneratePlan) -> dict[str, Any]:
        system = self._generate_system_prompt(plan)
        user = self._generate_user_prompt(plan)
        prepared, error = self._generate_request(plan, system, user)
        if error is not None:
            return error
        resource_allocation, error = self._generate_prepare_resources(
            plan, prepared
        )
        if error is not None:
            return error
        return self._generate_execute(plan, prepared, resource_allocation)

    def _generate_execute(
        self,
        plan: _GeneratePlan,
        prepared: dict[str, Any],
        resource_allocation: Any,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        context_attempts: list[dict[str, Any]] = []
        context_state: dict[str, int] = self._context_state_for(
            plan.selected_model, prepared["maximum_context"]
        )
        try:
            return self._generate_attempt_and_result(
                plan,
                prepared,
                context_state,
                context_attempts,
                started,
                resource_allocation,
            )
        except InterruptedError:
            return _generate_cancelled()
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
            return self._retry_automatic_route(
                plan,
                self._generate_failure_payload(
                    plan,
                    prepared,
                    context_attempts=context_attempts,
                    error=error,
                    started=started,
                ),
            )

    def _generate_attempt_and_result(
        self,
        plan: _GeneratePlan,
        prepared: dict[str, Any],
        context_state: dict[str, int],
        context_attempts: list[dict[str, Any]],
        started: float,
        resource_allocation: Any,
    ) -> dict[str, Any]:
        response, context_state, request_payload = self._generate_stream(
            plan, prepared, context_state, context_attempts
        )
        message = response.get("message")
        text = str(message.get("content") if isinstance(message, Mapping) else "").strip()
        if not 2 <= len(text) <= 64_000:
            raise RuntimeError("TRANSFORMER_OUTPUT_INVALID")
        unsupported = self._unsupported_facts(plan, text)
        if str(plan.intent) in self._STRICT_FACT_INTENTS and unsupported:
            return self._retry_automatic_route(plan, {
                "ok": False,
                "error_code": "TRANSFORMER_FACT_VALIDATION_FAILED",
                "unsupported_facts": unsupported,
                "fallback_required": True,
            })
        result = self._generate_success_result(
            plan,
            prepared,
            text=text,
            response=response,
            request_payload=request_payload,
            context_state=context_state,
            context_attempts=context_attempts,
            unsupported=unsupported,
            started=started,
            resource_allocation=resource_allocation,
        )
        if not plan.requested_model:
            result["automatic_model_route"] = self._automatic_route_report(plan)
        return result

    def _unsupported_facts(
        self, plan: _GeneratePlan, text: str
    ) -> dict[str, list[str]]:
        allowed_facts = self._fact_values(
            f"{plan.normalized_prompt}\n{plan.context}"
        )
        output_facts = self._fact_values(text)
        return {
            name: sorted(values - allowed_facts[name])
            for name, values in output_facts.items()
            if values - allowed_facts[name]
        }

    def _generate_stream(
        self,
        plan: _GeneratePlan,
        prepared: dict[str, Any],
        context_state: dict[str, int],
        context_attempts: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        request_payload = prepared["request_payload"]
        model_slot = self._model_inference_slots.get(plan.selected_model)
        with self._inference_slots, (
            model_slot if model_slot is not None else nullcontext()
        ):
            generation_timeout = self._generation_timeout(plan)
            while True:
                attempted_context = int(request_payload["options"]["num_ctx"])
                try:
                    response = self._generate_chat_request(
                        plan, request_payload, generation_timeout
                    )
                except InterruptedError:
                    raise
                except (OSError, ValueError, RuntimeError,
                        urllib.error.URLError) as error:
                    if not self._is_memory_pressure(error):
                        raise
                    retry = self._generate_pressure_retry(
                        plan,
                        prepared,
                        request_payload,
                        context_attempts,
                        attempted_context,
                    )
                    if retry is None:
                        raise
                    request_payload, context_state = retry
                    continue
                context_attempts.append({
                    "num_ctx": attempted_context,
                    "ok": True,
                    "memory_pressure": False,
                })
                context_state = self._record_context_success(
                    model=plan.selected_model,
                    used_context=attempted_context,
                    maximum=prepared["maximum_context"],
                )
                break
        return response, context_state, request_payload

    def _generate_pressure_retry(
        self,
        plan: _GeneratePlan,
        prepared: dict[str, Any],
        request_payload: dict[str, Any],
        context_attempts: list[dict[str, Any]],
        attempted_context: int,
    ) -> tuple[dict[str, Any], dict[str, int]] | None:
        next_context, context_state = (
            self._record_context_memory_pressure(
                model=plan.selected_model,
                failed_context=attempted_context,
                required_context=prepared["required_context"],
                maximum=prepared["maximum_context"],
            )
        )
        context_attempts.append(
            {
                "num_ctx": attempted_context,
                "ok": False,
                "memory_pressure": True,
            }
        )
        if next_context is None:
            return None
        if self._uses_default_transport:
            self.resource_manager.release_failed_model(plan.selected_model)
        return {
            **request_payload,
            "options": {
                **request_payload["options"],
                "num_ctx": next_context,
            },
        }, context_state

    def _generation_timeout(self, plan: _GeneratePlan) -> float:
        if plan.user_selected_model:
            return self.SELECTED_MODEL_GENERATION_TIMEOUT_SECONDS
        if str(plan.intent) == "self_upgrade":
            return self.SELF_UPGRADE_GENERATION_TIMEOUT_SECONDS
        return self.DEFAULT_GENERATION_TIMEOUT_SECONDS

    def _generate_chat_request(
        self,
        plan: _GeneratePlan,
        request_payload: dict[str, Any],
        generation_timeout: float,
    ) -> dict[str, Any]:
        if self._uses_default_transport:
            return self._http_chat_stream(
                f"{self.endpoint}/api/chat",
                request_payload,
                generation_timeout,
                plan.cancel_event,
                plan.progress_callback,
            )
        custom_payload = {
            **request_payload,
            "stream": False,
        }
        return self._transport(
            "POST",
            f"{self.endpoint}/api/chat",
            custom_payload,
            generation_timeout,
        )
