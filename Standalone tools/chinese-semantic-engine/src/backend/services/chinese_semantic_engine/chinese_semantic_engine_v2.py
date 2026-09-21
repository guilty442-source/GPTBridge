"""Chinese Semantic Engine v2 — Core Module.

Enhanced Chinese semantic processing engine with extensible architecture.
Integrates with GPTBridge governance, shared-layer contracts, and xingcheng domain.

Architecture:
- Component: chinese-semantic-engine (model, standalone-service)
- Sovereign: xingcheng-domain
- Dependencies: shared-layer, qdrant, local-model
- Information channels: information-channel
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from shared_layer.contracts.registry import CapabilityRegistry
from shared_layer.contracts.types import ModuleIdentity


COMPONENT_ID: Final[str] = "chinese-semantic-engine"
ARCHITECTURAL_ROLE: Final[str] = "model"
RUNTIME_FORM: Final[str] = "standalone-service"
OWNER_SOVEREIGN: Final[str] = "xingcheng-domain"
EXECUTION_IDENTITY: Final[str] = "chinese-semantic-engine-v2"
DEFAULT_CONFIG_PATH: Final[Path] = Path("config/chinese_semantic_engine.json")


class SemanticProcessingError(RuntimeError):
    """Base exception for semantic processing failures."""


class SemanticModelUnavailable(SemanticProcessingError):
    """Raised when the underlying semantic model is not available."""


class SemanticValidationError(SemanticProcessingError):
    """Raised when semantic validation fails."""


@dataclass(frozen=True)
class SemanticConfig:
    """Configuration for Chinese Semantic Engine v2.

    Attributes:
        model_endpoint: Target model endpoint identifier.
        qdrant_collection: Qdrant collection for semantic indexing.
        max_context_tokens: Maximum token context window.
        enable_fusion_retrieval: Enable dense+sparse+semantic fusion.
        fallback_enabled: Allow degraded-mode fallback.
        timeout_seconds: Request timeout.
    """
    model_endpoint: str = "xingcheng"
    qdrant_collection: str = "chinese-semantic-v2"
    max_context_tokens: int = 8192
    enable_fusion_retrieval: bool = True
    fallback_enabled: bool = True
    timeout_seconds: int = 30

    @classmethod
    def from_file(cls, path: Path) -> SemanticConfig:
        """Load configuration from JSON file."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    def to_file(self, path: Path) -> None:
        """Save configuration to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


@dataclass(frozen=True)
class SemanticRequest:
    """Input request for semantic processing.

    Attributes:
        text: Input Chinese text to process.
        operation: Operation type (analyze, embed, retrieve, synthesize).
        parameters: Operation-specific parameters.
        priority_class: Request priority (critical, interactive, background, maintenance).
        correlation_id: Request correlation identifier.
    """
    text: str
    operation: str
    parameters: dict[str, Any] = field(default_factory=dict)
    priority_class: str = "interactive"
    correlation_id: str = ""


@dataclass(frozen=True)
class SemanticResponse:
    """Output response from semantic processing.

    Attributes:
        result: Processing result payload.
        metadata: Processing metadata (timing, model info, etc.).
        warnings: Non-fatal warnings during processing.
        degraded: Whether response is from degraded mode.
    """
    result: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    degraded: bool = False


class SemanticProcessor(Protocol):
    """Protocol for semantic processing backends.

    Implementations must provide the core semantic operations.
    """

    async def analyze(self, request: SemanticRequest) -> SemanticResponse:
        """Perform semantic analysis on input text."""
        ...

    async def embed(self, request: SemanticRequest) -> SemanticResponse:
        """Generate semantic embeddings for input text."""
        ...

    async def retrieve(self, request: SemanticRequest) -> SemanticResponse:
        """Retrieve semantically relevant content."""
        ...

    async def synthesize(self, request: SemanticRequest) -> SemanticResponse:
        """Synthesize response from semantic understanding."""
        ...

    async def health_check(self) -> dict[str, Any]:
        """Return health status of the processor."""
        ...


class BaseSemanticProcessor(ABC):
    """Abstract base class for semantic processors.

    Provides common infrastructure and validation.
    Subclasses implement backend-specific logic.
    """

    def __init__(self, config: SemanticConfig) -> None:
        self._config = config
        self._initialized = False

    @property
    def config(self) -> SemanticConfig:
        return self._config

    @abstractmethod
    async def _initialize(self) -> None:
        """Initialize backend connections and resources."""

    @abstractmethod
    async def _shutdown(self) -> None:
        """Cleanup backend connections and resources."""

    @abstractmethod
    async def _analyze_impl(self, request: SemanticRequest) -> SemanticResponse:
        """Implementation of semantic analysis."""

    @abstractmethod
    async def _embed_impl(self, request: SemanticRequest) -> SemanticResponse:
        """Implementation of embedding generation."""

    @abstractmethod
    async def _retrieve_impl(self, request: SemanticRequest) -> SemanticResponse:
        """Implementation of semantic retrieval."""

    @abstractmethod
    async def _synthesize_impl(self, request: SemanticRequest) -> SemanticResponse:
        """Implementation of response synthesis."""

    async def initialize(self) -> None:
        """Initialize the processor (idempotent)."""
        if not self._initialized:
            await self._initialize()
            self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown the processor (idempotent)."""
        if self._initialized:
            await self._shutdown()
            self._initialized = False

    async def analyze(self, request: SemanticRequest) -> SemanticResponse:
        """Analyze Chinese text semantically."""
        await self.initialize()
        return await self._analyze_impl(request)

    async def embed(self, request: SemanticRequest) -> SemanticResponse:
        """Generate embeddings for Chinese text."""
        await self.initialize()
        return await self._embed_impl(request)

    async def retrieve(self, request: SemanticRequest) -> SemanticResponse:
        """Retrieve semantically relevant content."""
        await self.initialize()
        return await self._retrieve_impl(request)

    async def synthesize(self, request: SemanticRequest) -> SemanticResponse:
        """Synthesize response from semantic understanding."""
        await self.initialize()
        return await self._synthesize_impl(request)

    async def health_check(self) -> dict[str, Any]:
        """Return health status."""
        processor_info = None
        if self._processor is not None:
            processor_info = {
                "type": self._processor.__class__.__name__,
                "initialized": self._processor._initialized if hasattr(self._processor, '_initialized') else False,
                "model_available": getattr(self._processor, '_model_available', False),
            }
        return {
            "component": COMPONENT_ID,
            "version": "2.0.0",
            "initialized": self._initialized,
            "config": self._config.__dict__,
            "processor": processor_info,
        }


class ChineseSemanticEngineV2:
    """Main entry point for Chinese Semantic Engine v2.

    Orchestrates semantic processing through pluggable backends,
    manages configuration, and provides extension points.

    Extension Points:
        - Custom SemanticProcessor implementations
        - Pre/post processing hooks
        - Custom validation rules
        - Alternative storage backends
    """

    def __init__(
        self,
        config: SemanticConfig | None = None,
        processor: SemanticProcessor | None = None,
    ) -> None:
        self._config = config or SemanticConfig()
        self._processor = processor
        self._hooks: dict[str, list[callable]] = {
            "pre_analyze": [],
            "post_analyze": [],
            "pre_embed": [],
            "post_embed": [],
            "pre_retrieve": [],
            "post_retrieve": [],
            "pre_synthesize": [],
            "post_synthesize": [],
        }

    @property
    def config(self) -> SemanticConfig:
        return self._config

    @property
    def processor(self) -> SemanticProcessor | None:
        return self._processor

    def set_processor(self, processor: SemanticProcessor) -> None:
        """Set the semantic processor backend."""
        self._processor = processor

    def register_hook(self, hook_name: str, callback: callable) -> None:
        """Register a processing hook.

        Args:
            hook_name: One of pre_analyze, post_analyze, pre_embed, post_embed,
                       pre_retrieve, post_retrieve, pre_synthesize, post_synthesize
            callback: Async function receiving (request, response) for post hooks
                      or (request) for pre hooks
        """
        if hook_name not in self._hooks:
            raise ValueError(f"Unknown hook: {hook_name}")
        self._hooks[hook_name].append(callback)

    async def _run_pre_hooks(self, hook_name: str, request: SemanticRequest) -> SemanticRequest:
        """Run pre-processing hooks."""
        for hook in self._hooks[hook_name]:
            result = await hook(request)
            if result is not None:
                request = result
        return request

    async def _run_post_hooks(
        self, hook_name: str, request: SemanticRequest, response: SemanticResponse
    ) -> SemanticResponse:
        """Run post-processing hooks."""
        for hook in self._hooks[hook_name]:
            result = await hook(request, response)
            if result is not None:
                response = result
        return response

    async def analyze(self, request: SemanticRequest) -> SemanticResponse:
        """Perform semantic analysis on Chinese text.

        Args:
            request: Semantic analysis request

        Returns:
            Semantic analysis response

        Raises:
            SemanticProcessingError: If processing fails
            SemanticModelUnavailable: If model backend is unavailable
        """
        if not self._processor:
            raise SemanticModelUnavailable("No semantic processor configured")

        request = await self._run_pre_hooks("pre_analyze", request)
        response = await self._processor.analyze(request)
        response = await self._run_post_hooks("post_analyze", request, response)
        return response

    async def embed(self, request: SemanticRequest) -> SemanticResponse:
        """Generate semantic embeddings for Chinese text."""
        if not self._processor:
            raise SemanticModelUnavailable("No semantic processor configured")

        request = await self._run_pre_hooks("pre_embed", request)
        response = await self._processor.embed(request)
        response = await self._run_post_hooks("post_embed", request, response)
        return response

    async def retrieve(self, request: SemanticRequest) -> SemanticResponse:
        """Retrieve semantically relevant content."""
        if not self._processor:
            raise SemanticModelUnavailable("No semantic processor configured")

        request = await self._run_pre_hooks("pre_retrieve", request)
        response = await self._processor.retrieve(request)
        response = await self._run_post_hooks("post_retrieve", request, response)
        return response

    async def synthesize(self, request: SemanticRequest) -> SemanticResponse:
        """Synthesize response from semantic understanding."""
        if not self._processor:
            raise SemanticModelUnavailable("No semantic processor configured")

        request = await self._run_pre_hooks("pre_synthesize", request)
        response = await self._processor.synthesize(request)
        response = await self._run_post_hooks("post_synthesize", request, response)
        return response

    async def process(
        self,
        text: str,
        operation: str = "analyze",
        **parameters: Any
    ) -> SemanticResponse:
        """Convenience method for single-operation processing.

        Args:
            text: Input Chinese text
            operation: One of analyze, embed, retrieve, synthesize
            **parameters: Operation-specific parameters

        Returns:
            Semantic response
        """
        request = SemanticRequest(text=text, operation=operation, parameters=parameters)

        if operation == "analyze":
            return await self.analyze(request)
        elif operation == "embed":
            return await self.embed(request)
        elif operation == "retrieve":
            return await self.retrieve(request)
        elif operation == "synthesize":
            return await self.synthesize(request)
        else:
            raise SemanticValidationError(f"Unknown operation: {operation}")

    async def initialize(self) -> None:
        """Initialize the engine and its processor."""
        if self._processor:
            await self._processor.initialize()

    async def shutdown(self) -> None:
        """Shutdown the engine and its processor."""
        if self._processor:
            await self._processor.shutdown()

    async def health_check(self) -> dict[str, Any]:
        """Return comprehensive health status."""
        health = {
            "component": COMPONENT_ID,
            "version": "2.0.0",
            "config": self._config.__dict__,
            "processor": None,
        }
        if self._processor:
            health["processor"] = await self._processor.health_check()
        return health


class XingchengSemanticProcessor(BaseSemanticProcessor):
    """Xingcheng-native semantic processor implementation.

    Integrates with xingcheng model runtime via shared-layer contracts
    and governed information channels.
    """

    def __init__(
        self,
        config: SemanticConfig,
        capability_registry: CapabilityRegistry | None = None,
        module_identity: ModuleIdentity | None = None,
    ) -> None:
        super().__init__(config)
        self._capability_registry = capability_registry
        self._module_identity = module_identity or ModuleIdentity(module_id=COMPONENT_ID)
        self._qdrant_client = None
        self._model_runtime = None
        self._native_model = None
        self._model_available = False
        self._import_error: str | None = None

    async def _initialize(self) -> None:
        """Initialize xingcheng model runtime and Qdrant connections."""
        try:
            from shared_layer.adaptive import get_plane
            from shared_layer.local.vector_store import LocalVectorStore
            from pathlib import Path
            from services.xingcheng.infrastructure.native_model import StarNativeLanguageModel

            self._adaptive_plane = get_plane()

            if self._config.enable_fusion_retrieval:
                self._qdrant_client = LocalVectorStore(
                    root=Path.cwd(),
                    dimension=256,
                )
                self._qdrant_client.ensure_collection(256)

            # Initialize native model directly (no external runtime needed)
            self._native_model = StarNativeLanguageModel(model_role="main")
            self._model_runtime = self._native_model
            self._model_available = True
        except ImportError as e:
            # xingcheng modules not available in this tool's path
            # Per governance, tools must communicate via AI channel, not direct imports
            self._import_error = str(e)
            self._model_available = False
            # Don't raise - initialization succeeds but model is unavailable
            # Processing methods will raise SemanticModelUnavailable

    async def _shutdown(self) -> None:
        """Cleanup connections."""
        if self._qdrant_client:
            pass  # LocalVectorStore doesn't need explicit close
        if self._model_runtime:
            pass  # Native model doesn't need explicit close

    async def _connect_model_runtime(self) -> Any:
        """Connect to xingcheng model runtime via governed channel."""
        if not self._model_available:
            raise SemanticModelUnavailable(
                f"Xingcheng model not available: {self._import_error}"
            )
        return self._native_model

    async def _analyze_impl(self, request: SemanticRequest) -> SemanticResponse:
        """Analyze text using xingcheng semantic understanding."""
        if not self._model_available:
            raise SemanticModelUnavailable(
                f"Xingcheng model not available: {self._import_error}"
            )
        try:
            from services.xingcheng.infrastructure.chinese_semantic_engine import ChineseSemanticEngine
        except ImportError as e:
            raise SemanticModelUnavailable(f"Xingcheng module not available: {e}")

        engine = ChineseSemanticEngine()
        analysis = engine.analyze(request.text, context=request.parameters.get("context", ""))

        return SemanticResponse(
            result=analysis.to_semantic_plan(),
            metadata={"model": "xingcheng-native", "operation": "analyze"},
        )

    async def _embed_impl(self, request: SemanticRequest) -> SemanticResponse:
        """Generate embeddings using xingcheng model."""
        if not self._model_available:
            raise SemanticModelUnavailable(
                f"Xingcheng model not available: {self._import_error}"
            )
        try:
            from services.xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime
        except ImportError as e:
            raise SemanticModelUnavailable(f"Xingcheng module not available: {e}")

        runtime = StarTransformerRuntime(enabled=True)
        embeddings = runtime.embed(texts=[request.text])

        return SemanticResponse(
            result={"embeddings": embeddings},
            metadata={"model": "qwen3-embedding:4b", "operation": "embed", "dimension": len(embeddings[0]) if embeddings else 0},
        )

    async def _retrieve_impl(self, request: SemanticRequest) -> SemanticResponse:
        """Retrieve using hybrid dense+sparse+semantic fusion."""
        if not self._model_available:
            raise SemanticModelUnavailable(
                f"Xingcheng model not available: {self._import_error}"
            )
        if not self._qdrant_client:
            raise SemanticModelUnavailable("Qdrant not initialized")

        try:
            from services.xingcheng.infrastructure.rag.hybrid import HybridRetriever
        except ImportError as e:
            raise SemanticModelUnavailable(f"Xingcheng module not available: {e}")

        retriever = HybridRetriever(self._qdrant_client)
        results = await retriever.retrieve(
            query=request.text,
            method=request.parameters.get("method", "semantic-fusion"),
            top_k=request.parameters.get("top_k", 10),
        )

        return SemanticResponse(
            result={"results": results},
            metadata={"method": "hybrid-fusion", "collection": self._config.qdrant_collection},
        )

    async def _synthesize_impl(self, request: SemanticRequest) -> SemanticResponse:
        """Synthesize response from retrieved context."""
        from services.xingcheng.infrastructure.native_model import StarNativeLanguageModel

        context = request.parameters.get("context", [])
        context_text = "\n".join(str(c) for c in context) if isinstance(context, list) else str(context)

        model = StarNativeLanguageModel(model_role="main")
        result = model.language_model.generate(
            intent="conversation",
            prompt=request.text,
            grounding=context_text,
            max_tokens=request.parameters.get("max_tokens", 180),
            temperature=request.parameters.get("temperature", 0.55),
            top_k=request.parameters.get("top_k", 4),
        )

        return SemanticResponse(
            result={"synthesis": result.get("text", "")},
            metadata={"model": "xingcheng-native", "operation": "synthesize"},
        )

    async def health_check(self) -> dict[str, Any]:
        """Return processor health status."""
        base = await super().health_check()
        base.update({
            "processor_type": "xingcheng-native",
            "qdrant_connected": self._qdrant_client is not None,
            "model_runtime_connected": self._model_runtime is not None,
        })
        return base


def create_engine(
    config: SemanticConfig | None = None,
    processor: SemanticProcessor | None = None,
) -> ChineseSemanticEngineV2:
    """Factory function to create a configured ChineseSemanticEngineV2 instance.

    Args:
        config: Optional configuration (loads default if not provided)
        processor: Optional custom processor (creates XingchengSemanticProcessor if not provided)

    Returns:
        Configured ChineseSemanticEngineV2 instance
    """
    cfg = config or SemanticConfig()
    proc = processor or XingchengSemanticProcessor(cfg)
    return ChineseSemanticEngineV2(config=cfg, processor=proc)


async def create_initialized_engine(
    config: SemanticConfig | None = None,
    processor: SemanticProcessor | None = None,
) -> ChineseSemanticEngineV2:
    """Create and initialize a ChineseSemanticEngineV2 instance.

    Args:
        config: Optional configuration
        processor: Optional custom processor (creates XingchengSemanticProcessor if not provided)

    Returns:
        Initialized ChineseSemanticEngineV2 instance
    """
    engine = create_engine(config, processor)
    await engine.initialize()
    return engine


__all__ = [
    "COMPONENT_ID",
    "ARCHITECTURAL_ROLE",
    "RUNTIME_FORM",
    "OWNER_SOVEREIGN",
    "EXECUTION_IDENTITY",
    "DEFAULT_CONFIG_PATH",
    "SemanticProcessingError",
    "SemanticModelUnavailable",
    "SemanticValidationError",
    "SemanticConfig",
    "SemanticRequest",
    "SemanticResponse",
    "SemanticProcessor",
    "BaseSemanticProcessor",
    "ChineseSemanticEngineV2",
    "XingchengSemanticProcessor",
    "create_engine",
    "create_initialized_engine",
]