"""Cross-engine publish barrier.

Multi-engine data is invisible to readers until *all* engines agree.
``READY`` is the only readable state, and the aggregate consistency state is
the weakest link across engines (a missing Qdrant point means DEGRADED, not
READY).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from .types import Engine


class ResourceState(Enum):
    PREPARING = "PREPARING"
    INDEXING = "INDEXING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    TOMBSTONED = "TOMBSTONED"
    PURGED = "PURGED"
    FAILED = "FAILED"


READABLE_STATES: Final[frozenset[ResourceState]] = frozenset({ResourceState.READY})

_ALLOWED_TRANSITIONS: Final[dict[ResourceState, frozenset[ResourceState]]] = {
    ResourceState.PREPARING: frozenset(
        {ResourceState.INDEXING, ResourceState.FAILED, ResourceState.TOMBSTONED}
    ),
    ResourceState.INDEXING: frozenset(
        {ResourceState.VERIFYING, ResourceState.FAILED, ResourceState.TOMBSTONED}
    ),
    ResourceState.VERIFYING: frozenset(
        {ResourceState.READY, ResourceState.FAILED, ResourceState.TOMBSTONED}
    ),
    ResourceState.READY: frozenset({ResourceState.TOMBSTONED}),
    ResourceState.TOMBSTONED: frozenset({ResourceState.PURGED}),
    ResourceState.PURGED: frozenset(),
    ResourceState.FAILED: frozenset({ResourceState.TOMBSTONED}),
}


class BarrierError(RuntimeError):
    """Raised for invalid publish-barrier transitions or reads."""


@dataclass
class PublishBarrier:
    resource_id: str
    state: ResourceState = ResourceState.PREPARING
    history: list[tuple[str, str]] = field(default_factory=list)

    def transition(self, target: ResourceState, *, reason: str = "") -> ResourceState:
        allowed = _ALLOWED_TRANSITIONS[self.state]
        if target not in allowed:
            raise BarrierError(f"BARRIER_INVALID_TRANSITION:{self.state.value}->{target.value}")
        self.history.append((self.state.value, target.value))
        self.state = target
        if reason:
            self.history.append(("reason", reason))
        return self.state

    def readable(self) -> bool:
        return self.state in READABLE_STATES

    def assert_readable(self) -> None:
        if not self.readable():
            raise BarrierError(f"RESOURCE_NOT_PUBLISHED:{self.resource_id}:{self.state.value}")


class ConsistencyState(Enum):
    CONSISTENT = "CONSISTENT"
    PENDING = "PENDING"
    DEGRADED = "DEGRADED"
    RECONCILING = "RECONCILING"
    CONFLICT = "CONFLICT"
    ORPHANED = "ORPHANED"
    INVALID = "INVALID"


@dataclass(frozen=True)
class EngineEvidence:
    engine: Engine
    present: bool
    verified: bool = False
    revision: str = ""
    detail: str = ""


def evaluate_consistency(
    barrier_state: ResourceState,
    evidence: dict[Engine, EngineEvidence],
    *,
    reconciling: bool = False,
) -> ConsistencyState:
    """Aggregate the weakest engine into one consistency verdict."""
    if barrier_state is ResourceState.TOMBSTONED or barrier_state is ResourceState.PURGED:
        return ConsistencyState.PENDING
    if reconciling:
        return ConsistencyState.RECONCILING
    missing = [item for item in evidence.values() if not item.present]
    unverified = [item for item in evidence.values() if item.present and not item.verified]
    revisions = {item.revision for item in evidence.values() if item.present and item.revision}
    if missing:
        if barrier_state is ResourceState.READY:
            return ConsistencyState.DEGRADED
        return ConsistencyState.PENDING
    if len(revisions) > 1:
        return ConsistencyState.CONFLICT
    if unverified:
        return ConsistencyState.PENDING
    if barrier_state is ResourceState.READY:
        return ConsistencyState.CONSISTENT
    return ConsistencyState.PENDING


__all__ = [
    "BarrierError",
    "ConsistencyState",
    "EngineEvidence",
    "PublishBarrier",
    "READABLE_STATES",
    "ResourceState",
    "evaluate_consistency",
]
