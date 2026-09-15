"""Standardized Health Checks - A263 Contract Compliance.

Implements unified health check interface following Governance Codex A263
(channel-contract) requirements for all components.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

try:
    from typing import Self
except ImportError:
    from typing_extensions import Self


# ================================================================
# Health Status Enum
# ================================================================


class HealthStatus(Enum):
    """Standard health status values per A263."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

    def is_operational(self) -> bool:
        """Return True if status allows serving traffic."""
        return self in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)


# ================================================================
# Health Check Result
# ================================================================


@dataclass(frozen=True)
class HealthCheckResult:
    """Result of a single health check."""

    name: str
    status: HealthStatus
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    critical: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Export as dictionary."""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "timestamp": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
            "duration_ms": round(self.duration_ms, 2),
            "critical": self.critical,
        }

    @classmethod
    def healthy(cls, name: str, message: str = "", **details: Any) -> Self:
        return cls(name, HealthStatus.HEALTHY, message, details)

    @classmethod
    def degraded(cls, name: str, message: str = "", **details: Any) -> Self:
        return cls(name, HealthStatus.DEGRADED, message, details)

    @classmethod
    def unhealthy(cls, name: str, message: str = "", **details: Any) -> Self:
        return cls(name, HealthStatus.UNHEALTHY, message, details)

    @classmethod
    def unknown(cls, name: str, message: str = "", **details: Any) -> Self:
        return cls(name, HealthStatus.UNKNOWN, message, details)


# ================================================================
# Health Checker Interface
# ================================================================


class HealthChecker(ABC):
    """Abstract base for health checkers."""

    @property
    @abstractmethod
    def component_name(self) -> str:
        """Component identifier."""

    @property
    @abstractmethod
    def component_type(self) -> str:
        """Component type: sovereign|gateway|runtime|tool."""

    @abstractmethod
    async def check(self) -> list[HealthCheckResult]:
        """Run all health checks and return results."""

    async def check_single(self, name: str) -> HealthCheckResult | None:
        """Run a single named check (default: run all and filter)."""
        results = await self.check()
        for result in results:
            if result.name == name:
                return result
        return None


# ================================================================
# Standard Health Checker Implementation
# ================================================================


CheckFunc = Callable[[], Awaitable[HealthCheckResult] | HealthCheckResult]


class StandardHealthChecker(HealthChecker):
    """Standard health checker with registered check functions."""

    def __init__(
        self,
        component_name: str,
        component_type: str,
        version: str = "unknown",
    ) -> None:
        self._component_name = component_name
        self._component_type = component_type
        self._version = version
        self._checks: dict[str, tuple[CheckFunc, bool]] = {}  # name -> (func, critical)
        self._start_time = time.time()

    @property
    def component_name(self) -> str:
        return self._component_name

    @property
    def component_type(self) -> str:
        return self._component_type

    @property
    def version(self) -> str:
        return self._version

    def register_check(self, name: str, func: CheckFunc, critical: bool = True) -> Self:
        """Register a health check function."""
        self._checks[name] = (func, critical)
        return self

    def unregister_check(self, name: str) -> bool:
        """Unregister a health check."""
        return self._checks.pop(name, None) is not None

    async def check(self) -> list[HealthCheckResult]:
        """Run all registered health checks."""
        results: list[HealthCheckResult] = []

        for name, (func, critical) in self._checks.items():
            start = time.perf_counter()
            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func()
                else:
                    result = func()
                if not isinstance(result, HealthCheckResult):
                    result = HealthCheckResult.healthy(name, "check returned non-HealthCheckResult")
            except Exception as exc:
                result = HealthCheckResult.unhealthy(
                    name, f"check failed: {type(exc).__name__}: {exc}"
                )
            result = HealthCheckResult(
                name=result.name,
                status=result.status,
                message=result.message,
                details=result.details,
                timestamp=result.timestamp,
                duration_ms=(time.perf_counter() - start) * 1000,
                critical=critical,
            )
            results.append(result)

        return results

    async def overall_status(self) -> HealthStatus:
        """Compute overall health status."""
        results = await self.check()
        if not results:
            return HealthStatus.UNKNOWN

        # Any critical unhealthy -> unhealthy
        for result in results:
            if result.critical and result.status == HealthStatus.UNHEALTHY:
                return HealthStatus.UNHEALTHY

        # Any degraded -> degraded
        for result in results:
            if result.status == HealthStatus.DEGRADED:
                return HealthStatus.DEGRADED

        # All healthy -> healthy
        return HealthStatus.HEALTHY

    def to_dict(self, results: list[HealthCheckResult] | None = None) -> dict[str, Any]:
        """Export health status as dictionary."""
        if results is None:
            # Can't await here, caller should pass results
            results = []

        overall = HealthStatus.UNKNOWN
        if results:
            for r in results:
                if r.critical and r.status == HealthStatus.UNHEALTHY:
                    overall = HealthStatus.UNHEALTHY
                    break
            else:
                for r in results:
                    if r.status == HealthStatus.DEGRADED:
                        overall = HealthStatus.DEGRADED
                        break
                else:
                    overall = HealthStatus.HEALTHY

        return {
            "component": self._component_name,
            "component_type": self._component_type,
            "version": self._version,
            "status": overall.value,
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "checks": [r.to_dict() for r in results],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ================================================================
# A263 Health Contract
# ================================================================


class A263HealthContract:
    """A263-compliant health contract for channel-based components.

    Per Governance Codex A263 (channel-contract):
    - information-layer is the single owner of all channels
    - typed generation on every channel (channel_generation)
    - two-way heartbeat with deadline
    - bounded queue/backpressure
    - control priority channel
    - transactional outbox for state changes
    - ordered ack/cursor
    - disconnect invalidates ready
    - reconnect requires snapshot/cursor/hash convergence
    """

    # Required health check names for A263 compliance
    REQUIRED_CHECKS: tuple[str, ...] = (
        "channel_connectivity",
        "heartbeat_liveness",
        "generation_validity",
        "queue_backpressure",
        "control_channel",
        "outbox_integrity",
        "ack_cursor_ordering",
        "reconnect_capability",
    )

    # Optional/degradable checks
    DEGRADABLE_CHECKS: tuple[str, ...] = (
        "dependency_reachability",
        "resource_availability",
        "performance_baseline",
    )

    def __init__(self, component_name: str, channel_id: str) -> None:
        self.component_name = component_name
        self.channel_id = channel_id
        self._checker = StandardHealthChecker(component_name, "channel", "1.0")
        self._channel_state: dict[str, Any] = {}

    def update_channel_state(self, **state: Any) -> None:
        """Update observed channel state for health evaluation."""
        self._channel_state.update(state)

    def register_channel_checks(
        self,
        connectivity_check: Callable[[], Awaitable[bool] | bool],
        heartbeat_check: Callable[[], Awaitable[bool] | bool],
        generation_check: Callable[[], Awaitable[bool] | bool],
        queue_check: Callable[[], Awaitable[bool] | bool],
        control_check: Callable[[], Awaitable[bool] | bool],
        outbox_check: Callable[[], Awaitable[bool] | bool],
        ack_check: Callable[[], Awaitable[bool] | bool],
        reconnect_check: Callable[[], Awaitable[bool] | bool],
    ) -> None:
        """Register all required A263 channel health checks."""

        async def check_connectivity() -> HealthCheckResult:
            ok = await connectivity_check() if asyncio.iscoroutinefunction(connectivity_check) else connectivity_check()
            return HealthCheckResult.healthy("channel_connectivity") if ok else HealthCheckResult.unhealthy("channel_connectivity", "channel not connected")

        async def check_heartbeat() -> HealthCheckResult:
            ok = await heartbeat_check() if asyncio.iscoroutinefunction(heartbeat_check) else heartbeat_check()
            return HealthCheckResult.healthy("heartbeat_liveness") if ok else HealthCheckResult.unhealthy("heartbeat_liveness", "heartbeat deadline exceeded")

        async def check_generation() -> HealthCheckResult:
            ok = await generation_check() if asyncio.iscoroutinefunction(generation_check) else generation_check()
            return HealthCheckResult.healthy("generation_validity") if ok else HealthCheckResult.unhealthy("generation_validity", "generation mismatch")

        async def check_queue() -> HealthCheckResult:
            ok = await queue_check() if asyncio.iscoroutinefunction(queue_check) else queue_check()
            return HealthCheckResult.healthy("queue_backpressure") if ok else HealthCheckResult.degraded("queue_backpressure", "queue near capacity")

        async def check_control() -> HealthCheckResult:
            ok = await control_check() if asyncio.iscoroutinefunction(control_check) else control_check()
            return HealthCheckResult.healthy("control_channel") if ok else HealthCheckResult.unhealthy("control_channel", "control channel unavailable")

        async def check_outbox() -> HealthCheckResult:
            ok = await outbox_check() if asyncio.iscoroutinefunction(outbox_check) else outbox_check()
            return HealthCheckResult.healthy("outbox_integrity") if ok else HealthCheckResult.unhealthy("outbox_integrity", "outbox corrupted")

        async def check_ack() -> HealthCheckResult:
            ok = await ack_check() if asyncio.iscoroutinefunction(ack_check) else ack_check()
            return HealthCheckResult.healthy("ack_cursor_ordering") if ok else HealthCheckResult.degraded("ack_cursor_ordering", "ack cursor gap detected")

        async def check_reconnect() -> HealthCheckResult:
            ok = await reconnect_check() if asyncio.iscoroutinefunction(reconnect_check) else reconnect_check()
            return HealthCheckResult.healthy("reconnect_capability") if ok else HealthCheckResult.degraded("reconnect_capability", "reconnect not available")

        # Register required checks (critical)
        self._checker.register_check("channel_connectivity", check_connectivity, critical=True)
        self._checker.register_check("heartbeat_liveness", check_heartbeat, critical=True)
        self._checker.register_check("generation_validity", check_generation, critical=True)
        self._checker.register_check("queue_backpressure", check_queue, critical=False)
        self._checker.register_check("control_channel", check_control, critical=True)
        self._checker.register_check("outbox_integrity", check_outbox, critical=True)
        self._checker.register_check("ack_cursor_ordering", check_ack, critical=False)
        self._checker.register_check("reconnect_capability", check_reconnect, critical=False)

    @property
    def checker(self) -> StandardHealthChecker:
        return self._checker

    async def check(self) -> list[HealthCheckResult]:
        """Run all A263 health checks."""
        return await self._checker.check()

    async def overall_status(self) -> HealthStatus:
        return await self._checker.overall_status()

    def is_a263_compliant(self, results: list[HealthCheckResult]) -> bool:
        """Verify all required A263 checks pass."""
        required_names = set(self.REQUIRED_CHECKS)
        for result in results:
            if result.name in required_names:
                if result.critical and result.status != HealthStatus.HEALTHY:
                    return False
                required_names.discard(result.name)
        return len(required_names) == 0