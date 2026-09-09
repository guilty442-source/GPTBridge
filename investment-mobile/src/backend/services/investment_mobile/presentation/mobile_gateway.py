"""Mobile gateway compatibility shim.

The implementation has been subdivided into focused modules within this
package.  This module re-exports the public surface so that existing
``from ...presentation.mobile_gateway import ...`` statements keep working.
"""

from __future__ import annotations

from ._constants import (
    DEFAULT_PAIRING_TTL_HOURS,
    DEFAULT_PORT,
    MAX_REQUESTS_PER_WINDOW,
    PAIRING_ALPHABET,
    REQUEST_WINDOW_SECONDS,
    SESSION_IDLE_SECONDS,
    SESSION_TTL_HOURS,
    CommandScheduler,
    RemoteUrlProvider,
    SnapshotProvider,
)
from ._gateway import MobileSyncGateway
from ._templates import _mobile_html
from ._utils import _json_bytes, local_ipv4_addresses, normalize_remote_url

__all__ = [
    "PAIRING_ALPHABET",
    "DEFAULT_PORT",
    "DEFAULT_PAIRING_TTL_HOURS",
    "REQUEST_WINDOW_SECONDS",
    "MAX_REQUESTS_PER_WINDOW",
    "SESSION_TTL_HOURS",
    "SESSION_IDLE_SECONDS",
    "SnapshotProvider",
    "CommandScheduler",
    "RemoteUrlProvider",
    "normalize_remote_url",
    "local_ipv4_addresses",
    "_json_bytes",
    "_mobile_html",
    "MobileSyncGateway",
]
