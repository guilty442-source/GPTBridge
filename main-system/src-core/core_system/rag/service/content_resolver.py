"""Content Resolver — fetching original content is a formal,
authorized step, never a service-side file read.

    locator_id
      -> Permission Sovereign
      -> owning module's resolver
      -> authorized content

RAG never learns physical paths.  Even if Qdrant were read directly,
only opaque IDs are there — no path, no full text.  Code RAG and
Memory RAG each plug in their own resolver:

    CodeContentResolver   : locator -> repo/worktree/commit -> fn
    MemoryContentResolver : scope-gated memory broker
    FileContentResolver   : governed path owner
    DatabaseContentResolver
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ResolvedContent:
    locator_id: str
    content: str
    content_hash: str = ""
    version: int = 0
    resolver: str = ""
    authorized: bool = True
    # never carries physical path — provenance is symbolic
    provenance: dict[str, Any] = field(default_factory=dict)


class ContentResolver(Protocol):
    """One resolver per resource_type, owned by the owning module."""

    resource_type: str

    def resolve(
        self,
        actor_id: str,
        module_id: str,
        locator_id: str,
        version: int,
    ) -> ResolvedContent:
        ...


class ContentResolverRegistry:
    """Routes resolve() to the owning module's resolver; unknown
    resource types fail closed."""

    def __init__(self) -> None:
        self._resolvers: dict[str, ContentResolver] = {}

    def register(self, resolver: ContentResolver) -> None:
        self._resolvers[resolver.resource_type] = resolver

    def resolve(
        self,
        actor_id: str,
        module_id: str,
        resource_type: str,
        locator_id: str,
        version: int,
    ) -> ResolvedContent:
        resolver = self._resolvers.get(resource_type)
        if resolver is None:
            raise PermissionError(
                f"no content resolver registered for '{resource_type}'"
            )
        return resolver.resolve(actor_id, module_id, locator_id, version)


class DenyAllResolver:
    """Default for unimplemented types — fail closed."""

    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type

    def resolve(
        self, actor_id: str, module_id: str, locator_id: str, version: int
    ) -> ResolvedContent:
        raise PermissionError(f"resolver '{self.resource_type}' denied access")


__all__ = [
    "ContentResolver",
    "ContentResolverRegistry",
    "DenyAllResolver",
    "ResolvedContent",
]
