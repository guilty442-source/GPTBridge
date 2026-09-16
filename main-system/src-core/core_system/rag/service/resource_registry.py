"""RagResourceRegistry — the single resource identity system.

All four architectures (hybrid/code/memory/agentic) reference the
same registry; no RAG variant may mint its own resource ids.

RAG is the *index owner*, not the content owner — the registry holds
identity/classification/state, never content or physical paths
(opaque ``locator_id`` only; resolution goes through the owning
module's ContentResolver).
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RagResource:
    resource_id: str
    module_id: str
    owner_id: str
    resource_type: str           # document | code | memory | ...
    data_category: str
    locator_id: str
    classification: str = "internal"
    current_version: int = 1
    state: str = "ACTIVE"        # mirrors lifecycle states upstream
    registered_at: float = 0.0


class RagResourceRegistry:
    """In-memory canonical registry facade — the durable copy lives
    in PostgreSQL (structural authority); this is the service-side
    view the retrievers share."""

    def __init__(self, id_prefix: str = "res") -> None:
        self._lock = threading.Lock()
        self._resources: dict[str, RagResource] = {}
        self._counter = itertools.count(1)
        self._prefix = id_prefix

    def register(
        self,
        module_id: str,
        owner_id: str,
        resource_type: str,
        data_category: str,
        locator_id: str,
        *,
        classification: str = "internal",
    ) -> RagResource:
        with self._lock:
            rid = f"{self._prefix}-{next(self._counter):08d}"
            res = RagResource(
                resource_id=rid,
                module_id=module_id,
                owner_id=owner_id,
                resource_type=resource_type,
                data_category=data_category,
                locator_id=locator_id,
                classification=classification,
                registered_at=time.time(),
            )
            self._resources[rid] = res
            return res

    def get(self, resource_id: str) -> RagResource | None:
        return self._resources.get(resource_id)

    def set_state(self, resource_id: str, state: str) -> RagResource | None:
        with self._lock:
            res = self._resources.get(resource_id)
            if res is None:
                return None
            import dataclasses
            res = dataclasses.replace(res, state=state)
            self._resources[resource_id] = res
            return res

    def bump_version(self, resource_id: str) -> RagResource | None:
        with self._lock:
            res = self._resources.get(resource_id)
            if res is None:
                return None
            import dataclasses
            res = dataclasses.replace(res, current_version=res.current_version + 1)
            self._resources[resource_id] = res
            return res

    def for_module(self, module_id: str) -> list[RagResource]:
        return [r for r in self._resources.values() if r.module_id == module_id]


__all__ = ["RagResource", "RagResourceRegistry"]
