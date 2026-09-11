"""Backward-compatible re-export module for the vaultly platform adapters.

The original monolithic ``adapters.py`` has been split into focused modules:

* :mod:`scan_scripts`       – JavaScript snippets for DOM scanning/navigation
* :mod:`post_scripts`       – JavaScript snippets for post discovery/inspection
* :mod:`constants`          – shared constants (handle patterns, app IDs, …)
* :mod:`star_candidate`     – star-candidate classification logic
* :mod:`platform_definition`– ``PlatformDefinition`` dataclass and ``PLATFORMS``
* :mod:`adapter_base`       – ``PlatformAdapterBase`` mixin
* :mod:`adapter_cookies`    – ``CookieMixin`` (cookie + Instagram API helpers)
* :mod:`adapter_instagram`  – ``InstagramNavigationMixin`` (DOM navigation)
* :mod:`adapter_scan`       – ``FollowingScanMixin`` (scan/prepare logic)
* :mod:`adapter`            – ``PlatformAdapter`` and ``get_adapter``

This module re-exports every public name so that existing imports such as::

    from ..integration.adapters import PLATFORMS, get_adapter

continue to work unchanged.
"""

from __future__ import annotations

from .adapter import PlatformAdapter, get_adapter
from .adapter_base import PlatformAdapterBase
from .adapter_cookies import CookieMixin
from .adapter_instagram import InstagramNavigationMixin
from .adapter_scan import FollowingScanMixin
from .constants import (
    HANDLE_PATTERNS,
    IGNORED_HANDLES,
    INSTAGRAM_API_PAGE_SIZE,
    INSTAGRAM_WEB_APP_ID,
)
from .platform_definition import PLATFORMS, PlatformDefinition
from .post_scripts import DISCOVER_POSTS_SCRIPT, INSPECT_POST_SCRIPT
from .navigation_scripts import (
    AUTO_SCAN_CONTEXT_SCRIPT,
    INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT,
    INSTAGRAM_PROFILE_NAVIGATION_SCRIPT,
    INSTAGRAM_PROFILE_SETTINGS_SCRIPT,
    OPEN_FOLLOWING_LIST_SCRIPT,
)
from .scan_scripts import (
    FOLLOWING_SCAN_SCRIPT,
    RESET_FOLLOWING_SCROLL_SCRIPT,
    SCROLL_SCRIPT,
)
from .star_candidate import (
    NON_STAR_ACCOUNT_PHRASES,
    NON_STAR_ACCOUNT_WORDS,
    STAR_ACCOUNT_PHRASES,
    STAR_ACCOUNT_WORDS,
    is_star_candidate_account,
)

__all__ = [
    # Platform definitions
    "PlatformDefinition",
    "PLATFORMS",
    # Adapter classes
    "PlatformAdapter",
    "PlatformAdapterBase",
    "CookieMixin",
    "InstagramNavigationMixin",
    "FollowingScanMixin",
    # Factory
    "get_adapter",
    # Constants
    "IGNORED_HANDLES",
    "HANDLE_PATTERNS",
    "INSTAGRAM_WEB_APP_ID",
    "INSTAGRAM_API_PAGE_SIZE",
    # Star candidate
    "STAR_ACCOUNT_WORDS",
    "STAR_ACCOUNT_PHRASES",
    "NON_STAR_ACCOUNT_WORDS",
    "NON_STAR_ACCOUNT_PHRASES",
    "is_star_candidate_account",
    # Scan scripts
    "FOLLOWING_SCAN_SCRIPT",
    "SCROLL_SCRIPT",
    "RESET_FOLLOWING_SCROLL_SCRIPT",
    "AUTO_SCAN_CONTEXT_SCRIPT",
    "INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT",
    "INSTAGRAM_PROFILE_NAVIGATION_SCRIPT",
    "OPEN_FOLLOWING_LIST_SCRIPT",
    "INSTAGRAM_PROFILE_SETTINGS_SCRIPT",
    # Post scripts
    "DISCOVER_POSTS_SCRIPT",
    "INSPECT_POST_SCRIPT",
]
