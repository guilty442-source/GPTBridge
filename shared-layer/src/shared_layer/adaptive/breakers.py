"""Per-domain circuit breakers.

One failure domain must not condemn the whole SQL layer: transport, index,
audit, reconcile and rag_metadata each get an independent breaker.
"""

from __future__ import annotations

import threading
from typing import Final

from ..resilient_circuit import CircuitBreaker, CircuitOpenError, CircuitState

DOMAINS: Final[tuple[str, ...]] = (
    "transport",
    "index",
    "audit",
    "reconcile",
    "rag_metadata",
)


class DomainBreakerRegistry:
    """Thread-safe registry of independent, per-domain circuit breakers."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        domains: tuple[str, ...] = DOMAINS,
    ) -> None:
        self._lock = threading.RLock()
        self._breakers: dict[str, CircuitBreaker] = {
            domain: CircuitBreaker(
                name=domain,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
            for domain in domains
        }

    def _breaker(self, domain: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(domain)
            if breaker is None:
                raise KeyError(f"unknown circuit domain: {domain}")
            return breaker

    def allow(self, domain: str) -> bool:
        """True when the domain may issue a call right now."""
        return self._breaker(domain).state is not CircuitState.OPEN

    def guard(self, domain: str) -> None:
        """Raise :class:`CircuitOpenError` when the domain is open."""
        if not self.allow(domain):
            raise CircuitOpenError(f"circuit '{domain}' is open (fail-closed)")

    def call(self, domain: str, fn, *args, **kwargs):
        return self._breaker(domain).call(fn, *args, **kwargs)

    def record_success(self, domain: str) -> None:
        self._breaker(domain)._on_success()

    def record_failure(self, domain: str) -> None:
        self._breaker(domain)._on_failure()

    def reset(self, domain: str | None = None) -> None:
        with self._lock:
            if domain is None:
                for breaker in self._breakers.values():
                    breaker.reset()
                return
            self._breaker(domain).reset()

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {name: breaker.stats() for name, breaker in self._breakers.items()}


def get_default_registry() -> DomainBreakerRegistry:
    return _DEFAULT_REGISTRY


_DEFAULT_REGISTRY: Final[DomainBreakerRegistry] = DomainBreakerRegistry()


__all__ = ["DOMAINS", "DomainBreakerRegistry", "get_default_registry"]
