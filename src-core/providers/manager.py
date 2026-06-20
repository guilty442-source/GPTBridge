from __future__ import annotations

from collections.abc import Callable
from typing import Any


ProviderFactory = Callable[[], Any]


class ProviderManager:
    """Registry for AI provider instances and factories."""

    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}
        self._factories: dict[str, ProviderFactory] = {}

    @staticmethod
    def _normalize_name(name: str) -> str:
        key = str(name or "").strip().lower()
        if not key:
            raise ValueError("provider name is required")
        return key

    def register(self, name: str, provider: Any) -> None:
        self._providers[self._normalize_name(name)] = provider

    def register_factory(self, name: str, factory: ProviderFactory) -> None:
        if not callable(factory):
            raise TypeError("provider factory must be callable")
        self._factories[self._normalize_name(name)] = factory

    def get(self, name: str) -> Any | None:
        key = self._normalize_name(name)
        if key not in self._providers and key in self._factories:
            self._providers[key] = self._factories[key]()
        return self._providers.get(key)

    def require(self, name: str) -> Any:
        provider = self.get(name)
        if provider is None:
            raise KeyError(f"provider is not registered: {name}")
        return provider

    def list_names(self) -> list[str]:
        return sorted(set(self._providers) | set(self._factories))

    def status(self) -> dict[str, Any]:
        return {
            "registered": self.list_names(),
            "loaded": sorted(self._providers),
            "factory_count": len(self._factories),
        }
