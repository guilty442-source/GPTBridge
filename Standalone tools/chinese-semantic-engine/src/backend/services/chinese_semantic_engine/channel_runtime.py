"""Chinese Semantic Engine v2 — Channel Runtime.

Information channel gateway for the Chinese Semantic Engine v2 component.
Provides governed communication via shared-layer information channels.

Architecture:
- Component: chinese-semantic-engine (model, standalone-service)
- Sovereign: xingcheng-domain
- Information channels: information-channel
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from shared_layer.contracts.registry import CapabilityRegistry
from shared_layer.contracts.types import ModuleIdentity

from .chinese_semantic_engine_v2 import (
    ChineseSemanticEngineV2,
    SemanticConfig,
    SemanticRequest,
    SemanticResponse,
    SemanticProcessingError,
    SemanticModelUnavailable,
)


@dataclass(frozen=True)
class ChannelMessage:
    """Message envelope for information channel communication."""
    channel: str
    payload: dict[str, Any]
    correlation_id: str = ""
    reply_to: str = ""


class ChannelRuntime:
    """Channel runtime for Chinese Semantic Engine v2.

    Manages information channel subscriptions, message routing,
    and integration with the semantic engine.
    """

    def __init__(
        self,
        engine: ChineseSemanticEngineV2,
        module_identity: ModuleIdentity,
        capability_registry: CapabilityRegistry | None = None,
    ) -> None:
        self._engine = engine
        self._module_identity = module_identity
        self._capability_registry = capability_registry
        self._handlers: dict[str, Callable[[ChannelMessage], Awaitable[ChannelMessage | None]]] = {}
        self._running = False
        self._subscriber_task: asyncio.Task | None = None

    @property
    def engine(self) -> ChineseSemanticEngineV2:
        return self._engine

    @property
    def module_identity(self) -> ModuleIdentity:
        return self._module_identity

    def register_handler(
        self,
        channel: str,
        handler: Callable[[ChannelMessage], Awaitable[ChannelMessage | None]],
    ) -> None:
        """Register a message handler for a channel."""
        self._handlers[channel] = handler

    async def start(self) -> None:
        """Start the channel runtime."""
        if self._running:
            return

        await self._engine.initialize()
        self._running = True
        self._subscriber_task = asyncio.create_task(self._subscriber_loop())

    async def stop(self) -> None:
        """Stop the channel runtime."""
        if not self._running:
            return

        self._running = False
        if self._subscriber_task:
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except asyncio.CancelledError:
                pass
        await self._engine.shutdown()

    async def _subscriber_loop(self) -> None:
        """Main subscriber loop for information channels."""
        from shared_layer.workflow.runtime import get_workflow_executor

        executor = get_workflow_executor()
        subscriber = await executor.create_subscriber(
            module_id=self._module_identity.module_id,
            channels=["information-channel"],
        )

        async for message in subscriber:
            if not self._running:
                break
            await self._dispatch_message(message)

    async def _dispatch_message(self, raw_message: dict[str, Any]) -> None:
        """Dispatch incoming message to registered handler."""
        try:
            message = ChannelMessage(
                channel=raw_message.get("channel", ""),
                payload=raw_message.get("payload", {}),
                correlation_id=raw_message.get("correlation_id", ""),
                reply_to=raw_message.get("reply_to", ""),
            )

            handler = self._handlers.get(message.channel)
            if handler:
                response = await handler(message)
                if response and message.reply_to:
                    await self._send_response(message.reply_to, response)
        except Exception as e:
            await self._send_error(message.reply_to if 'message' in locals() else "", str(e))

    async def _send_response(self, reply_to: str, response: ChannelMessage) -> None:
        """Send response message."""
        from shared_layer.workflow.runtime import get_workflow_executor

        executor = get_workflow_executor()
        await executor.publish(
            module_id=self._module_identity.module_id,
            channel=reply_to,
            payload=response.payload,
            correlation_id=response.correlation_id,
        )

    async def _send_error(self, reply_to: str, error: str) -> None:
        """Send error response."""
        await self._send_response(reply_to, ChannelMessage(
            channel="error",
            payload={"error": error, "component": "chinese-semantic-engine"},
        ))


async def create_channel_runtime(
    config: SemanticConfig | None = None,
    module_identity: ModuleIdentity | None = None,
    capability_registry: CapabilityRegistry | None = None,
) -> ChannelRuntime:
    """Create and initialize a ChannelRuntime instance.

    Args:
        config: Semantic engine configuration
        module_identity: Module identity for channel registration
        capability_registry: Optional capability registry

    Returns:
        Initialized ChannelRuntime instance
    """
    engine = await create_initialized_engine(config)
    identity = module_identity or ModuleIdentity(module_id="chinese-semantic-engine")
    runtime = ChannelRuntime(engine, identity, capability_registry)

    # Register default handlers
    runtime.register_handler("semantic.analyze", _handle_analyze)
    runtime.register_handler("semantic.embed", _handle_embed)
    runtime.register_handler("semantic.retrieve", _handle_retrieve)
    runtime.register_handler("semantic.synthesize", _handle_synthesize)
    runtime.register_handler("semantic.health", _handle_health)

    await runtime.start()
    return runtime


async def _handle_analyze(message: ChannelMessage) -> ChannelMessage:
    """Handle semantic analysis request."""
    from .chinese_semantic_engine_v2 import ChineseSemanticEngineV2

    engine: ChineseSemanticEngineV2 = message.payload.get("_engine")
    if not engine:
        raise SemanticModelUnavailable("Engine not available in message context")

    request = SemanticRequest(
        text=message.payload.get("text", ""),
        operation="analyze",
        parameters=message.payload.get("parameters", {}),
        correlation_id=message.correlation_id,
    )

    response = await engine.analyze(request)
    return ChannelMessage(
        channel="semantic.analyze.response",
        payload={"result": response.result, "metadata": response.metadata},
        correlation_id=message.correlation_id,
        reply_to=message.reply_to,
    )


async def _handle_embed(message: ChannelMessage) -> ChannelMessage:
    """Handle embedding generation request."""
    from .chinese_semantic_engine_v2 import ChineseSemanticEngineV2

    engine: ChineseSemanticEngineV2 = message.payload.get("_engine")
    if not engine:
        raise SemanticModelUnavailable("Engine not available in message context")

    request = SemanticRequest(
        text=message.payload.get("text", ""),
        operation="embed",
        parameters=message.payload.get("parameters", {}),
        correlation_id=message.correlation_id,
    )

    response = await engine.embed(request)
    return ChannelMessage(
        channel="semantic.embed.response",
        payload={"result": response.result, "metadata": response.metadata},
        correlation_id=message.correlation_id,
        reply_to=message.reply_to,
    )


async def _handle_retrieve(message: ChannelMessage) -> ChannelMessage:
    """Handle semantic retrieval request."""
    from .chinese_semantic_engine_v2 import ChineseSemanticEngineV2

    engine: ChineseSemanticEngineV2 = message.payload.get("_engine")
    if not engine:
        raise SemanticModelUnavailable("Engine not available in message context")

    request = SemanticRequest(
        text=message.payload.get("text", ""),
        operation="retrieve",
        parameters=message.payload.get("parameters", {}),
        correlation_id=message.correlation_id,
    )

    response = await engine.retrieve(request)
    return ChannelMessage(
        channel="semantic.retrieve.response",
        payload={"result": response.result, "metadata": response.metadata},
        correlation_id=message.correlation_id,
        reply_to=message.reply_to,
    )


async def _handle_synthesize(message: ChannelMessage) -> ChannelMessage:
    """Handle response synthesis request."""
    from .chinese_semantic_engine_v2 import ChineseSemanticEngineV2

    engine: ChineseSemanticEngineV2 = message.payload.get("_engine")
    if not engine:
        raise SemanticModelUnavailable("Engine not available in message context")

    request = SemanticRequest(
        text=message.payload.get("text", ""),
        operation="synthesize",
        parameters=message.payload.get("parameters", {}),
        correlation_id=message.correlation_id,
    )

    response = await engine.synthesize(request)
    return ChannelMessage(
        channel="semantic.synthesize.response",
        payload={"result": response.result, "metadata": response.metadata},
        correlation_id=message.correlation_id,
        reply_to=message.reply_to,
    )


async def _handle_health(message: ChannelMessage) -> ChannelMessage:
    """Handle health check request."""
    from .chinese_semantic_engine_v2 import ChineseSemanticEngineV2

    engine: ChineseSemanticEngineV2 = message.payload.get("_engine")
    if not engine:
        raise SemanticModelUnavailable("Engine not available in message context")

    health = await engine.health_check()
    return ChannelMessage(
        channel="semantic.health.response",
        payload=health,
        correlation_id=message.correlation_id,
        reply_to=message.reply_to,
    )


__all__ = [
    "ChannelMessage",
    "ChannelRuntime",
    "create_channel_runtime",
]