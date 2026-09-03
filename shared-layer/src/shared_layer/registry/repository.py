from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlite3 import Connection

from ..local.registry_repository import (
    LocalResourceRegistry,
    LocationRecord,
    ResourceRegistry,
)


class LocalResourceRegistryAdapter(LocalResourceRegistry):
    """Backward-compatible adapter exposing the sqlite-based registry under
    the same ``ResourceRegistry`` class name used by callers."""


# Re-export the local implementation so legacy imports keep working.
ResourceRegistry = ResourceRegistry

__all__ = ["LocationRecord", "ResourceRegistry"]