from __future__ import annotations

from ..local.registry_repository import (
    LocationRecord,
    ResourceRegistry,
)

# Re-export the local implementation so legacy imports keep working.
__all__ = ["LocationRecord", "ResourceRegistry"]
