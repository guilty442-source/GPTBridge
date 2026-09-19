from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


JsonTransport = Callable[[str, str, dict[str, Any] | None, float], dict[str, Any]]


def _resource_preparation_lock(method):
    """Serialize only GPU resource preparation; allow Ollama HTTP calls in parallel.

    The lock is acquired only around ResourceManager.prepare_model() inside
    generate(), not around the entire generate() call.  This allows concurrent
    inference calls to proceed in parallel while preventing model load/unload
    races.
    """

    def guarded(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return method(self, *args, **kwargs)

    return guarded


@dataclass
class _GeneratePlan:
    """Immutable request context shared by the decomposed generate() helpers.

    Replaces the closure variables previously captured by the nested
    retry_automatic_route() and by the single-model inference tail of
    generate().
    """

    prompt: str
    intent: str
    model_role: str
    output: Mapping[str, Any]
    max_tokens: Any
    temperature: Any
    top_k: Any
    reasoning_effort: Any
    requested_model: Any
    response_format: Any
    cancel_event: Any
    progress_callback: Any
    release_after_generate: bool
    automatic_model_override: bool
    use_immutable_base: bool
    base_default_retry: bool
    normalized_task_intensity: str
    normalized_reasoning_effort: str
    selected_model: str
    user_selected_model: bool
    user_designated_model: bool
    visual_inputs: list[str]
    model_catalog: Mapping[str, Any]
    normalized_prompt: str
    context: str
    selected_metadata: Mapping[str, Any]
    parameter_settings: Mapping[str, Any]


def _generate_request_args(
    prompt: Any,
    intent: Any,
    model_role: Any,
    output: Any,
    max_tokens: Any,
    temperature: Any,
    top_k: Any,
    reasoning_effort: Any,
    task_intensity: Any,
    requested_model: Any,
    images: Any,
    complex_pipeline: Any,
    reasoning_pipeline: Any,
    division_pipeline: Any,
    cancel_event: Any,
    progress_callback: Any,
    response_format: Any,
    release_after_generate: Any,
    automatic_model_override: Any,
    use_immutable_base: Any,
    base_default_retry: Any,
    user_designated_model: Any = False,
) -> dict[str, Any]:
    """Pack the raw generate() arguments into one request mapping."""
    return {
        "prompt": prompt,
        "intent": intent,
        "model_role": model_role,
        "output": output,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_k": top_k,
        "reasoning_effort": reasoning_effort,
        "task_intensity": task_intensity,
        "requested_model": requested_model,
        "images": images,
        "complex_pipeline": complex_pipeline,
        "reasoning_pipeline": reasoning_pipeline,
        "division_pipeline": division_pipeline,
        "cancel_event": cancel_event,
        "progress_callback": progress_callback,
        "response_format": response_format,
        "release_after_generate": release_after_generate,
        "automatic_model_override": automatic_model_override,
        "use_immutable_base": use_immutable_base,
        "base_default_retry": base_default_retry,
        "user_designated_model": user_designated_model,
    }
