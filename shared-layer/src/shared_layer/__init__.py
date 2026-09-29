"""Shared layer package — import-cheap entry point.

The runtime hot path frequently needs a single leaf (e.g.
``shared_layer.performance.process_metrics`` or ``shared_layer.store``)
without paying for the channel/authentication stack.  Every public name
therefore resolves lazily (PEP 562); importing this package alone only
defines the resolver.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY_ATTRS: dict[str, str] = {
    "LRUCache": ".cache",
    "AsyncCache": ".cache",
    "cache_key": ".cache",
    "cached": ".cache",
    "SharedLayerChannel": ".channel",
    "GovernedRequestClient": ".request_client",
    "SharedLayerStore": ".store",
    "PLATFORM_ID": ".resource_identity",
    "ResourceIdentity": ".resource_identity",
    "XINGCHENG_MODULE_ID": ".resource_identity",
    "GovernedLocatorResolver": ".locator",
    "RegistryLocatorResolver": ".locator",
    "ResolvedOwnerResource": ".locator",
    "ModuleLocatorRepository": ".module_locator_repository",
    "SharedLayerStartup": ".startup",
    "StartupReport": ".startup",
}


def __getattr__(name: str) -> Any:
    """Lazily resolve a public shared-layer name (PEP 562)."""
    module_name = _LAZY_ATTRS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY_ATTRS])


__all__ = (
    "AsyncCache",
    "GovernedRequestClient",
    "LRUCache",
    "SharedLayerChannel",
    "SharedLayerStore",
    "PLATFORM_ID",
    "ResourceIdentity",
    "XINGCHENG_MODULE_ID",
    "GovernedLocatorResolver",
    "RegistryLocatorResolver",
    "ResolvedOwnerResource",
    "ModuleLocatorRepository",
    "SharedLayerStartup",
    "StartupReport",
    "cache_key",
    "cached",
)
