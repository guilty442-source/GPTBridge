"""Adaptive retry classification.

Different failures need different recovery strategies:

  * connection refused / unavailable -> exponential backoff
  * deadlock detected               -> short backoff, retry
  * permission denied               -> never retry (governance denial)
  * schema mismatch / drift         -> fail closed (no retry, no degradation)
  * serialization conflict          -> limited retry (bounded attempts)
  * everything else                 -> fail closed by default
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class RetryKind(Enum):
    EXPONENTIAL_BACKOFF = "exponential_backoff"
    SHORT_BACKOFF = "short_backoff"
    LIMITED_RETRY = "limited_retry"
    NO_RETRY = "no_retry"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class RetryDecision:
    kind: RetryKind
    should_retry: bool
    delay_seconds: float
    fail_closed: bool
    reason: str


_PERMANENT_MARKERS: Final[tuple[str, ...]] = (
    "permission denied",
    "permissiondenied",
    "insufficientprivilege",
    "not authorized",
    "denied",
)

_FAIL_CLOSED_MARKERS: Final[tuple[str, ...]] = (
    "schema mismatch",
    "schemadrift",
    "contract_version",
    "contract mismatch",
    "generation fence",
    "generationfence",
    "immutable",
    "read-only",
    "readonly",
    "append-only",
)

_SERIALIZATION_MARKERS: Final[tuple[str, ...]] = (
    "could not serialize",
    "serialization failure",
    "serialization_failure",
    "40001",
)

_DEADLOCK_MARKERS: Final[tuple[str, ...]] = (
    "deadlock detected",
    "deadlock",
    "40p01",
)

_CONNECTION_MARKERS: Final[tuple[str, ...]] = (
    "connection refused",
    "connectionrefused",
    "could not connect",
    "connection reset",
    "server closed the connection",
    "connection is closed",
    "pool exhausted",
    "too many clients",
    "timeout expired",
    "connection timeout",
)


@dataclass
class AdaptiveRetryPolicy:
    """Bounded retry policy with per-error backoff strategies."""

    exponential_base_seconds: float = 0.2
    exponential_attempts: int = 5
    exponential_max_seconds: float = 8.0
    short_backoff_seconds: float = 0.05
    short_backoff_attempts: int = 3
    limited_retry_seconds: float = 0.1
    limited_retry_attempts: int = 2
    jitter_seconds: float = 0.0

    def classify(self, error: BaseException) -> RetryKind:
        if isinstance(error, PermissionError):
            return RetryKind.NO_RETRY
        text = f"{type(error).__name__}: {error}".casefold()
        if any(marker in text for marker in _FAIL_CLOSED_MARKERS):
            return RetryKind.FAIL_CLOSED
        if any(marker in text for marker in _PERMANENT_MARKERS):
            return RetryKind.NO_RETRY
        if any(marker in text for marker in _SERIALIZATION_MARKERS):
            return RetryKind.LIMITED_RETRY
        if any(marker in text for marker in _DEADLOCK_MARKERS):
            return RetryKind.SHORT_BACKOFF
        if isinstance(error, (ConnectionError, TimeoutError, OSError)):
            return RetryKind.EXPONENTIAL_BACKOFF
        if any(marker in text for marker in _CONNECTION_MARKERS):
            return RetryKind.EXPONENTIAL_BACKOFF
        return RetryKind.FAIL_CLOSED

    def decide(self, error: BaseException, attempt: int) -> RetryDecision:
        """Attempt numbers are 1-based (the attempt that just failed)."""
        kind = self.classify(error)
        if kind is RetryKind.NO_RETRY:
            return RetryDecision(kind, False, 0.0, False, "permission-denied")
        if kind is RetryKind.FAIL_CLOSED:
            return RetryDecision(kind, False, 0.0, True, "schema-or-integrity-guard")
        if kind is RetryKind.LIMITED_RETRY:
            if attempt >= self.limited_retry_attempts:
                return RetryDecision(kind, False, 0.0, False, "serialization-retry-exhausted")
            return RetryDecision(
                kind, True, self.limited_retry_seconds + self.jitter_seconds,
                False, "serialization-conflict",
            )
        if kind is RetryKind.SHORT_BACKOFF:
            if attempt >= self.short_backoff_attempts:
                return RetryDecision(kind, False, 0.0, False, "deadlock-retry-exhausted")
            return RetryDecision(
                kind, True, self.short_backoff_seconds + self.jitter_seconds,
                False, "deadlock",
            )
        if attempt >= self.exponential_attempts:
            return RetryDecision(kind, False, 0.0, False, "connection-retry-exhausted")
        delay = min(
            self.exponential_base_seconds * (2.0 ** (attempt - 1)),
            self.exponential_max_seconds,
        )
        return RetryDecision(
            kind, True, delay + self.jitter_seconds, False, "connection-unavailable",
        )


__all__ = [
    "AdaptiveRetryPolicy",
    "RetryDecision",
    "RetryKind",
]
