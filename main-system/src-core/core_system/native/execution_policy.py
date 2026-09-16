"""Native Batching & Parallel Execution V1 — per-capability policy.

One governed policy object per native capability.  Only parser /
vector / transformer (the approved, benchmark-evidenced set) may hold
a policy; no other capability may create one.

    min/preferred/max batch size   — batching first
    max_batch_bytes                — memory ceiling per batch call
    parallel_threshold             — min items before parallel pays
    chunk policy                   — how work splits across workers
    worker limit / queue limit     — bounded, never unbounded
    deadline / cancellation        — original request deadline flows
                                     to every worker; cancellation is
                                     cooperative between chunks

Production worker counts and dispatch thresholds come from the real
benchmark matrix — never from ``hardware_concurrency`` directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class QueueVerdict(str, Enum):
    """Typed status returned to the Python policy layer — the C++/pool
    never makes the wait/fallback/reject business decision."""

    ACCEPTED = "accepted"
    BACKPRESSURE = "backpressure"      # queue near limit — wait advised
    REJECTED = "rejected"              # queue full — caller decides


class ChunkPolicy(str, Enum):
    """How a batch splits across workers.

    CONTIGUOUS  — block ranges (transform: cache-friendly slices)
    INDEPENDENT — whole items only (parser: never splits one stream)
    SCALAR      — element ranges   (vector: dims/pairs)
    """

    CONTIGUOUS = "contiguous"
    INDEPENDENT = "independent"
    SCALAR = "scalar"


@dataclass(frozen=True, slots=True)
class NativeExecutionPolicy:
    capability: str

    min_batch: int = 1
    preferred_batch: int = 32
    max_batch: int = 1024
    max_batch_bytes: int = 64 * 1024 * 1024

    parallel_threshold: int = 0        # 0 = never parallel
    chunk_policy: ChunkPolicy = ChunkPolicy.INDEPENDENT

    max_workers: int = 1               # 1 = single-thread baseline
    queue_limit: int = 256

    deadline_ms: int = 30_000
    cancel_check_every: int = 1        # chunks between cancel checks


APPROVED_CAPABILITIES = frozenset({"parser", "vector", "transform"})

# Baseline policies — worker limits are benchmarks placeholders;
# production values must come from run_parallel_benchmark evidence.
DEFAULT_POLICIES: dict[str, NativeExecutionPolicy] = {
    # parser: only parallel across independent inputs — a single
    # stateful stream is never split.
    "parser": NativeExecutionPolicy(
        capability="parser",
        preferred_batch=64,
        parallel_threshold=64,
        chunk_policy=ChunkPolicy.INDEPENDENT,
        max_workers=1,
    ),
    # vector: batch + SIMD-friendly element ranges first.
    "vector": NativeExecutionPolicy(
        capability="vector",
        preferred_batch=128,
        parallel_threshold=256,
        chunk_policy=ChunkPolicy.SCALAR,
        max_workers=1,
    ),
    # transform: contiguous/block-based parallel.
    "transform": NativeExecutionPolicy(
        capability="transform",
        preferred_batch=16,
        parallel_threshold=32,
        chunk_policy=ChunkPolicy.CONTIGUOUS,
        max_workers=1,
    ),
}


def policy_for(capability: str) -> NativeExecutionPolicy:
    """Only approved/benchmarked capabilities may execute through the
    native batching layer."""
    if capability not in APPROVED_CAPABILITIES:
        raise PermissionError(
            f"capability '{capability}' not approved for native execution"
        )
    return DEFAULT_POLICIES[capability]


def with_workers(
    policy: NativeExecutionPolicy, workers: int
) -> NativeExecutionPolicy:
    """Set the production worker count — caller supplies benchmark
    evidence; values are clamped to a sane bound, never derived from
    hardware_concurrency."""
    import dataclasses
    workers = max(1, min(int(workers), 16))
    return dataclasses.replace(policy, max_workers=workers)


def split_batch(
    items: int, policy: NativeExecutionPolicy
) -> list[tuple[int, int]]:
    """Split ``items`` into disjoint (start, end) slices honouring the
    chunk policy — outputs are pre-allocated per slice, never shared."""
    if items <= 0:
        return []
    workers = 1
    if policy.parallel_threshold and items >= policy.parallel_threshold:
        workers = policy.max_workers
    workers = max(1, min(workers, items))
    size = items // workers
    rem = items % workers
    slices: list[tuple[int, int]] = []
    start = 0
    for i in range(workers):
        n = size + (1 if i < rem else 0)
        slices.append((start, start + n))
        start += n
    return slices


__all__ = [
    "APPROVED_CAPABILITIES",
    "ChunkPolicy",
    "DEFAULT_POLICIES",
    "NativeExecutionPolicy",
    "QueueVerdict",
    "policy_for",
    "split_batch",
    "with_workers",
]
