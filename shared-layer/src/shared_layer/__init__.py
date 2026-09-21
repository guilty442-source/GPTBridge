from .cache import LRUCache, AsyncCache, cache_key, cached
from .channel import SharedLayerChannel
from .request_client import GovernedRequestClient
from .store import SharedLayerStore
from .resource_identity import PLATFORM_ID, ResourceIdentity, XINGCHENG_MODULE_ID
from .locator import GovernedLocatorResolver, RegistryLocatorResolver, ResolvedOwnerResource
from .module_locator_repository import ModuleLocatorRepository
from .startup import SharedLayerStartup, StartupReport


def __getattr__(name: str):
    # tokenizer 依賴 jieba；惰性導出避免 import shared_layer 在無 jieba 的
    # runtime（例如 GPU 協調路徑）整包失敗（G86 回歸）。
    if name in {"TokenizerWrapper", "Token", "create_tokenizer"}:
        from . import tokenizer as _tokenizer
        return getattr(_tokenizer, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    "TokenizerWrapper",
    "Token",
    "create_tokenizer",
)