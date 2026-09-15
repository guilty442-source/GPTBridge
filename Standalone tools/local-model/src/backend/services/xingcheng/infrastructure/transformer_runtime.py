from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .model_parameter_policy import ModelParameterPolicy
from .resource_manager import ResourceManager
from .transformer_runtime_catalog import TransformerRuntimeCatalog
from .transformer_runtime_checkpoint_repository import (
    TransformerRuntimeCheckpointRepository,
)
from .transformer_runtime_context import TransformerRuntimeContextMixin
from .transformer_runtime_http import TransformerRuntimeHttpMixin
from .transformer_runtime_inference import TransformerRuntimeInferenceMixin
from .transformer_runtime_model_metadata import TransformerRuntimeMetadata
from .transformer_runtime_pipeline import TransformerRuntimePipelineMixin
from .transformer_runtime_request import TransformerRuntimeRequestMixin
from .transformer_runtime_result import TransformerRuntimeResultMixin
from .transformer_runtime_routing import TransformerRuntimeRoutingMixin
from .transformer_runtime_status import TransformerRuntimeStatusMixin
from .transformer_runtime_support import (  # noqa: F401
    JsonTransport,
    _resource_preparation_lock,
)


class StarTransformerRuntime(
    TransformerRuntimeCatalog,
    TransformerRuntimeMetadata,
    TransformerRuntimeHttpMixin,
    TransformerRuntimeContextMixin,
    TransformerRuntimeStatusMixin,
    TransformerRuntimeRoutingMixin,
    TransformerRuntimePipelineMixin,
    TransformerRuntimeRequestMixin,
    TransformerRuntimeInferenceMixin,
    TransformerRuntimeResultMixin,
):
    """Governed loopback adapter for Star's quantized Transformer foundation model."""

    MODEL = TransformerRuntimeCatalog.MODEL
    DEFAULT_ENDPOINT = TransformerRuntimeCatalog.DEFAULT_ENDPOINT

    def __init__(
        self,
        *,
        enabled: bool = False,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = MODEL,
        transport: JsonTransport | None = None,
        checkpoint_root: Path | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.endpoint = self._validated_endpoint(endpoint)
        self.model = str(model or self.MODEL).strip()
        if self.model != self.MODEL:
            raise ValueError("TRANSFORMER_MODEL_NOT_GOVERNED")
        self._uses_default_transport = transport is None
        self._transport = transport or self._http_json
        self.resource_manager = ResourceManager(
            endpoint=self.endpoint,
            transport=self._transport,
            resident_models=self.RESIDENT_MODELS,
        )
        self.parameter_policy = ModelParameterPolicy()
        self._lock = threading.Lock()
        self._resource_lock = threading.Lock()
        self._probe_cache_ttl = 5.0  # seconds; avoids repeated /api/tags probes
        self._param_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._param_cache_ttl = 3.0  # seconds; avoids repeated stat/JSON/deepcopy
        self._inference_slots = threading.BoundedSemaphore(
            self.MAX_CONCURRENT_TRANSFORMERS
        )
        self._model_inference_slots = {
            self.COMMAND_UNDERSTANDING_MODEL: threading.BoundedSemaphore(
                self.COMMANDER_MAX_PARALLEL
            )
        }
        self._context_states: dict[str, dict[str, int]] = {}
        self._checkpoint_repository: (
            TransformerRuntimeCheckpointRepository | None
        ) = None
        if checkpoint_root is not None:
            self.configure_checkpoint_store(checkpoint_root)
        self._status = self._initial_status()

    def _initial_status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": False,
            "model_installed": False,
            "model": self.model,
            "endpoint_scope": "loopback-only",
            "remote_network_allowed": False,
            "last_error": "not-probed" if self.enabled else "disabled",
            "selectable_models": [],
        }

    def configure_checkpoint_store(self, tool_root: Path) -> None:
        repository = TransformerRuntimeCheckpointRepository(tool_root)
        with self._lock:
            self._checkpoint_repository = repository
            for model in self.KNOWN_MODEL_METADATA:
                stored = repository.load_context_state(model)
                if stored is not None:
                    self._context_states[model] = stored


__all__ = ["StarTransformerRuntime"]
