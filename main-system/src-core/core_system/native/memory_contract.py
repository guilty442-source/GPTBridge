"""Native Memory & Buffer Architecture V1 — Python canonical owner.

Python owns call sequencing, input policy, deadlines, cancellation and
result use (A204/A213/A214/A220).  This module is the governed contract
every native capability call passes through; the C ABI stays the sole
public interface, pybind11 stays binding-only, and the C++ core keeps
its private primitives in ``native/core/memory.hpp``.

Four ownership classes (mirroring memory.hpp):

    BORROWED_READONLY        — Python owns; native reads only
    BORROWED_MUTABLE         — Python owns; native may write in place
    NATIVE_OWNED             — native owns; RAII frees; never escapes
    CALLER_PROVIDED_OUTPUT   — Python pre-allocates; native writes

Rules:
    - every size/shape/stride is overflow-checked and budget-checked
      before any allocation is requested
    - allocator and deallocator live in the same owner; cross-runtime
      free is rejected outright
    - preference order: batching > buffer reuse > validated borrowed
      view > zero-copy (zero-copy needs profile evidence)
    - pybind11 handles buffer validation, lifetime, conversion, GIL —
      never algorithms or caches; this module never duplicates compute
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class BufferOwnership(str, Enum):
    BORROWED_READONLY = "BORROWED_READONLY"
    BORROWED_MUTABLE = "BORROWED_MUTABLE"
    NATIVE_OWNED = "NATIVE_OWNED"
    CALLER_PROVIDED_OUTPUT = "CALLER_PROVIDED_OUTPUT"


class BufferError(ValueError):
    """Invalid spec / ownership violation / budget exceeded."""


_INT64_MAX = (1 << 63) - 1

# Default workspace ceiling per call — oversized inputs are rejected
# before any allocation request reaches the boundary.
DEFAULT_WORKSPACE_BUDGET_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BufferSpec:
    """A validated buffer shape.  ``byte_size`` is only computable
    through ``validate`` — never trust raw caller math."""

    dtype_size: int
    shape: tuple[int, ...]
    ownership: BufferOwnership
    strides: tuple[int, ...] = ()

    @property
    def element_count(self) -> int:
        total = 1
        for d in self.shape:
            total *= d
        return total


def validate_buffer_spec(
    dtype_size: int,
    shape: tuple[int, ...],
    ownership: BufferOwnership,
    *,
    strides: tuple[int, ...] = (),
    budget_bytes: int = DEFAULT_WORKSPACE_BUDGET_BYTES,
) -> BufferSpec:
    """Overflow + budget validation before any allocation.  Raises
    BufferError (never allocates) on violation."""
    if dtype_size <= 0:
        raise BufferError("dtype_size must be positive")
    if ownership not in BufferOwnership:
        raise BufferError(f"unknown ownership {ownership!r}")
    if any(d < 0 for d in shape):
        raise BufferError("negative dimension")
    elems = 1
    for d in shape:
        elems *= d
        if elems > _INT64_MAX:
            raise BufferError("element count overflows int64")
    byte_size = elems * dtype_size
    if byte_size > _INT64_MAX:
        raise BufferError("byte size overflows int64")
    if byte_size > budget_bytes:
        raise BufferError(
            f"buffer {byte_size}B exceeds budget {budget_bytes}B"
        )
    if strides and len(strides) != len(shape):
        raise BufferError("strides length must equal shape rank")
    if strides and any(s < 0 for s in strides):
        raise BufferError("negative stride")
    return BufferSpec(dtype_size, tuple(shape), ownership, tuple(strides))


class TransferPolicy(str, Enum):
    """Preference order — cheap, safe transports first."""

    BATCH = "batch"                       # group small calls
    BUFFER_REUSE = "buffer-reuse"         # recycled caller buffer
    BORROWED_VIEW = "borrowed-view"       # validated view, no copy
    ZERO_COPY = "zero-copy"               # only with profile evidence


def select_transfer_policy(
    *,
    zero_copy_profiled: bool = False,
    reusable_buffer: bool = False,
    batchable: bool = False,
) -> TransferPolicy:
    """Decide transport.  Zero-copy is never chosen without recorded
    profile evidence; borrowed views are the safe default."""
    if batchable:
        return TransferPolicy.BATCH
    if reusable_buffer:
        return TransferPolicy.BUFFER_REUSE
    if zero_copy_profiled:
        return TransferPolicy.ZERO_COPY
    return TransferPolicy.BORROWED_VIEW


# ----------------------------------------------------------------------
# Lease registry — boundary lifetime tracking.
#
# Every buffer handed across the boundary is leased; the registry
# detects double-release, use-after-release and leaks.  The allocator/
# deallocator owner is recorded so cross-runtime frees are rejected.
# ----------------------------------------------------------------------
class LeaseState(str, Enum):
    LIVE = "live"
    RELEASED = "released"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class BufferLease:
    lease_id: int
    owner: str                      # runtime that allocated: "python"|"native"
    ownership: BufferOwnership
    spec: BufferSpec
    state: LeaseState
    acquired_at: float


class LeaseRegistry:
    """Thread-safe boundary lease ledger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[int, BufferLease] = {}
        self._ids = itertools.count(1)
        self._live_bytes = 0

    def acquire(
        self, spec: BufferSpec, *, owner: str = "python"
    ) -> BufferLease:
        with self._lock:
            lid = next(self._ids)
            lease = BufferLease(
                lease_id=lid,
                owner=owner,
                ownership=spec.ownership,
                spec=spec,
                state=LeaseState.LIVE,
                acquired_at=time.monotonic(),
            )
            self._leases[lid] = lease
            self._live_bytes += spec.element_count * spec.dtype_size
            return lease

    def release(self, lease: BufferLease, *, by: str = "python") -> None:
        """Same-owner release only — a native-owned buffer can never
        be freed by the Python side and vice versa."""
        with self._lock:
            cur = self._leases.get(lease.lease_id)
            if cur is None:
                raise BufferError("release of unknown lease")
            if cur.state is not LeaseState.LIVE:
                raise BufferError(
                    f"double-release of lease {lease.lease_id} ({cur.state.value})"
                )
            if by != cur.owner:
                raise BufferError(
                    f"cross-runtime free rejected: allocated by {cur.owner}, "
                    f"released by {by}"
                )
            import dataclasses
            self._leases[lease.lease_id] = dataclasses.replace(
                cur, state=LeaseState.RELEASED
            )
            self._live_bytes -= (
                cur.spec.element_count * cur.spec.dtype_size
            )

    def cancel(self, lease: BufferLease) -> None:
        """Cancellation path — releases the lease without use."""
        with self._lock:
            cur = self._leases.get(lease.lease_id)
            if cur is None:
                raise BufferError("cancel of unknown lease")
            if cur.state is not LeaseState.LIVE:
                return  # already released/cancelled — idempotent
            import dataclasses
            self._leases[lease.lease_id] = dataclasses.replace(
                cur, state=LeaseState.CANCELLED
            )
            self._live_bytes -= (
                cur.spec.element_count * cur.spec.dtype_size
            )

    def check_use(self, lease: BufferLease) -> None:
        """Guard every access — raises on use-after-release."""
        cur = self._leases.get(lease.lease_id)
        if cur is None or cur.state is not LeaseState.LIVE:
            raise BufferError(
                f"use-after-release of lease {lease.lease_id}"
            )

    @property
    def live_count(self) -> int:
        return sum(
            1 for l in self._leases.values() if l.state is LeaseState.LIVE
        )

    @property
    def live_bytes(self) -> int:
        return self._live_bytes


# ----------------------------------------------------------------------
# Per-capability metrics — the five required counters.
# ----------------------------------------------------------------------
@dataclass(slots=True)
class CapabilityMemoryProfile:
    capability: str
    boundary_copy_bytes: int = 0
    allocation_count: int = 0
    allocated_bytes: int = 0
    peak_workspace: int = 0
    retained_capacity: int = 0
    call_count: int = 0
    _live: int = field(default=0, repr=False)

    def record_call(
        self,
        *,
        copy_bytes: int = 0,
        allocations: int = 0,
        allocated_bytes: int = 0,
        workspace_bytes: int = 0,
        retained_bytes: int = 0,
    ) -> None:
        self.call_count += 1
        self.boundary_copy_bytes += max(0, copy_bytes)
        self.allocation_count += max(0, allocations)
        self.allocated_bytes += max(0, allocated_bytes)
        self._live += max(0, workspace_bytes)
        if self._live > self.peak_workspace:
            self.peak_workspace = self._live
        self.retained_capacity = max(0, retained_bytes)

    def record_workspace_free(self, bytes_freed: int) -> None:
        self._live = max(0, self._live - max(0, bytes_freed))

    def snapshot(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "call_count": self.call_count,
            "boundary_copy_bytes": self.boundary_copy_bytes,
            "allocation_count": self.allocation_count,
            "allocated_bytes": self.allocated_bytes,
            "peak_workspace": self.peak_workspace,
            "retained_capacity": self.retained_capacity,
        }


class MemoryMetrics:
    """Registry of per-capability memory profiles."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profiles: dict[str, CapabilityMemoryProfile] = {}

    def profile(self, capability: str) -> CapabilityMemoryProfile:
        with self._lock:
            p = self._profiles.get(capability)
            if p is None:
                p = CapabilityMemoryProfile(capability)
                self._profiles[capability] = p
            return p

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: p.snapshot() for k, p in self._profiles.items()}


class NativeCapabilitySession:
    """Wraps one native capability call: validate -> lease -> call ->
    record -> release.  Cancellation and failure paths always release.
    """

    def __init__(
        self,
        registry: LeaseRegistry,
        metrics: MemoryMetrics,
        capability: str,
        *,
        budget_bytes: int = DEFAULT_WORKSPACE_BUDGET_BYTES,
    ) -> None:
        self._registry = registry
        self._metrics = metrics
        self._capability = capability
        self._budget = budget_bytes

    def run(
        self,
        fn: Callable[..., Any],
        input_specs: list[BufferSpec],
        output_spec: Optional[BufferSpec] = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        leases = [
            self._registry.acquire(s, owner="python") for s in input_specs
        ]
        out_lease = (
            self._registry.acquire(output_spec, owner="python")
            if output_spec is not None
            else None
        )
        profile = self._metrics.profile(self._capability)
        copy_bytes = sum(
            s.element_count * s.dtype_size for s in input_specs
        )
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            if out_lease is not None:
                self._registry.cancel(out_lease)
            for lease in leases:
                self._registry.cancel(lease)
            raise
        finally:
            profile.record_call(copy_bytes=copy_bytes)
        for lease in leases:
            self._registry.release(lease)
        if out_lease is not None:
            self._registry.release(out_lease)
        return result


__all__ = [
    "BufferError",
    "BufferLease",
    "BufferOwnership",
    "BufferSpec",
    "CapabilityMemoryProfile",
    "DEFAULT_WORKSPACE_BUDGET_BYTES",
    "LeaseRegistry",
    "LeaseState",
    "MemoryMetrics",
    "NativeCapabilitySession",
    "TransferPolicy",
    "select_transfer_policy",
    "validate_buffer_spec",
]
