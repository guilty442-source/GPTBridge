"""Chinese Semantic Engine v2 — Main Entry Point.

Standalone service entry point for the Chinese Semantic Engine v2.
Can be run as a governed tool via ToolboxService or as a standalone process.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from shared_layer.contracts.types import ModuleIdentity

from .chinese_semantic_engine import (
    ChineseSemanticEngineV2,
    SemanticConfig,
    create_initialized_engine,
)
from .chinese_semantic_engine.channel_runtime import ChannelRuntime, create_channel_runtime


DEFAULT_CONFIG_PATH = Path("config/chinese_semantic_engine.json")


class ChineseSemanticEngineService:
    """Main service class for Chinese Semantic Engine v2.

    Manages lifecycle, configuration, and channel runtime.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path or DEFAULT_CONFIG_PATH
        self._config: SemanticConfig | None = None
        self._engine: ChineseSemanticEngineV2 | None = None
        self._channel_runtime: ChannelRuntime | None = None
        self._shutdown_event = asyncio.Event()

    async def initialize(self) -> None:
        """Initialize the service."""
        self._config = self._load_config()
        self._engine = await create_initialized_engine(self._config)

        identity = ModuleIdentity(module_id="chinese-semantic-engine")
        self._channel_runtime = await create_channel_runtime(
            config=self._config,
            module_identity=identity,
        )

        self._setup_signal_handlers()

    def _load_config(self) -> SemanticConfig:
        """Load configuration from file or create default."""
        if self._config_path.exists():
            return SemanticConfig.from_file(self._config_path)
        else:
            config = SemanticConfig()
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            config.to_file(self._config_path)
            return config

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except NotImplementedError:
                pass  # Windows doesn't support add_signal_handler

    async def run(self) -> None:
        """Run the service until shutdown signal."""
        if not self._engine or not self._channel_runtime:
            raise RuntimeError("Service not initialized")

        print(f"Chinese Semantic Engine v2 started (config: {self._config_path})")
        await self._shutdown_event.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        """Shutdown the service gracefully."""
        print("Shutting down Chinese Semantic Engine v2...")
        if self._channel_runtime:
            await self._channel_runtime.stop()
        if self._engine:
            await self._engine.shutdown()
        print("Shutdown complete")


async def main() -> int:
    """Main entry point."""
    service = ChineseSemanticEngineService()
    try:
        await service.initialize()
        await service.run()
        return 0
    except Exception as e:
        print(f"Service error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))