from __future__ import annotations

from .adapter_base import PlatformAdapterBase
from .adapter_cookies import CookieMixin
from .adapter_instagram import InstagramNavigationMixin
from .adapter_scan import FollowingScanMixin
from .platform_definition import PLATFORMS, PlatformDefinition


class PlatformAdapter(
    CookieMixin,
    InstagramNavigationMixin,
    FollowingScanMixin,
    PlatformAdapterBase,
):
    """Concrete platform adapter combining all mixin capabilities."""


def get_adapter(platform: str) -> PlatformAdapter:
    definition = PLATFORMS.get(platform)
    if definition is None:
        raise ValueError(f"不支援的平台：{platform}")
    return PlatformAdapter(definition)
