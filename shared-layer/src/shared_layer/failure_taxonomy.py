"""Error, Cancellation & Failure Semantics Consolidation V1.

Consolidates all failure handling into a single canonical taxonomy:

    VALIDATION_ERROR     — input failed validation
    CONTRACT_ERROR       — contract/precondition violation
    NOT_FOUND            — resource not found
    CONFLICT             — state conflict (duplicate, version mismatch)
    UNAUTHORIZED        — authentication required/failed
    FORBIDDEN            — authenticated but not permitted
    RESOURCE_EXHAUSTED   — quota/pool/memory exhausted
    UNAVAILABLE          — service temporarily unavailable
    TIMEOUT              — deadline elapsed (distinct from CANCELLED)
    CANCELLED            — explicit cancellation request
    PERSISTENCE_ERROR    — database/storage failure
    NATIVE_ERROR         — C/C++ native failure (mapped at ABI)
    PLATFORM_ERROR       — OS/.NET/Win32/platform failure
    INTERNAL_ERROR       — unexpected internal failure

Rules:
    - Python is the sole application failure/retry/fallback policy authority.
    - C++ exceptions are caught at the C ABI and mapped to stable status codes.
    - No exception/RTTI/STL/HRESULT/raw DB error leaks across language boundaries.
    - TypeScript only receives language-neutral typed API errors.
    - All sub-operations share one original monotonic deadline (remaining budget).
    - Cancellation propagates one-way from Python/request authority to native/SQL/C#.
    - Retry is decided by Python orchestration only (retryable + idempotent + budget).
    - Native fallback follows error classification — no blanket `except Exception`.
    - Invalid input, timeout, cancelled, contract/correctness failures are never hidden.

Codex basis:
    A204/A220 — C ABI boundary (no exception leak)
    A205 — API boundary (typed API errors)
    A211 — six-language canonical roles (Python = failure authority)
    A219 — Python fallback always preserved (but classified)
    A358 — benchmark evidence (FailureRecord)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Canonical failure taxonomy (versioned)
# ---------------------------------------------------------------------------

FAILURE_TAXONOMY_VERSION = "1.0"


class FailureCategory(str, Enum):
    """Canonical failure categories.

    These are the only failure types that cross language boundaries.
    All raw errors (Python exceptions, SQL driver errors, C#/.NET/Win32
    failures, native status codes) must be mapped to one of these.
    """
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"
    NATIVE_ERROR = "NATIVE_ERROR"
    PLATFORM_ERROR = "PLATFORM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# All valid categories (for validation)
ALL_CATEGORIES: frozenset[FailureCategory] = frozenset(FailureCategory)


# ---------------------------------------------------------------------------
# Native status codes (stable C ABI mapping)
# ---------------------------------------------------------------------------

class NativeStatus(int, Enum):
    """Stable status codes returned across the C ABI boundary.

    C++ exceptions are caught at the ABI and mapped to these codes.
    No exception/RTTI/STL/HRESULT leaks across the boundary.
    """
    OK = 0
    INVALID_ARGUMENT = 1      # → VALIDATION_ERROR
    OUT_OF_RANGE = 2           # → VALIDATION_ERROR
    NOT_FOUND = 3              # → NOT_FOUND
    ALREADY_EXISTS = 4         # → CONFLICT
    PERMISSION_DENIED = 5      # → FORBIDDEN
    UNAUTHENTICATED = 6        # → UNAUTHORIZED
    RESOURCE_EXHAUSTED = 7     # → RESOURCE_EXHAUSTED
    UNAVAILABLE = 8            # → UNAVAILABLE
    DEADLINE_EXCEEDED = 9      # → TIMEOUT
    CANCELLED = 10             # → CANCELLED
    INTERNAL = 99             # → INTERNAL_ERROR
    UNKNOWN = 100              # → INTERNAL_ERROR


# Mapping from native status codes to failure categories
_NATIVE_STATUS_TO_CATEGORY: dict[NativeStatus, FailureCategory] = {
    NativeStatus.OK: FailureCategory.INTERNAL_ERROR,  # not an error
    NativeStatus.INVALID_ARGUMENT: FailureCategory.VALIDATION_ERROR,
    NativeStatus.OUT_OF_RANGE: FailureCategory.VALIDATION_ERROR,
    NativeStatus.NOT_FOUND: FailureCategory.NOT_FOUND,
    NativeStatus.ALREADY_EXISTS: FailureCategory.CONFLICT,
    NativeStatus.PERMISSION_DENIED: FailureCategory.FORBIDDEN,
    NativeStatus.UNAUTHENTICATED: FailureCategory.UNAUTHORIZED,
    NativeStatus.RESOURCE_EXHAUSTED: FailureCategory.RESOURCE_EXHAUSTED,
    NativeStatus.UNAVAILABLE: FailureCategory.UNAVAILABLE,
    NativeStatus.DEADLINE_EXCEEDED: FailureCategory.TIMEOUT,
    NativeStatus.CANCELLED: FailureCategory.CANCELLED,
    NativeStatus.INTERNAL: FailureCategory.INTERNAL_ERROR,
    NativeStatus.UNKNOWN: FailureCategory.INTERNAL_ERROR,
}


# ---------------------------------------------------------------------------
# Typed exception hierarchy (Python canonical)
# ---------------------------------------------------------------------------

class CanonicalFailure(Exception):
    """Base class for all canonical failures.

    Python is the sole application failure policy authority.  All
    failures — from Python, SQL, native, C#, or platform — are mapped
    to a CanonicalFailure subclass with a FailureCategory.
    """

    category: FailureCategory = FailureCategory.INTERNAL_ERROR

    def __init__(
        self,
        message: str = "",
        *,
        category: FailureCategory | None = None,
        retryable: bool | None = None,
        idempotent: bool | None = None,
        cause: BaseException | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category
        self._retryable = retryable
        self._idempotent = idempotent
        self.cause = cause
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        """Whether this failure is retryable (Python orchestration decides)."""
        if self._retryable is not None:
            return self._retryable
        return _DEFAULT_RETRYABLE.get(self.category, False)

    @property
    def idempotent(self) -> bool:
        """Whether the operation that caused this failure is idempotent."""
        if self._idempotent is not None:
            return self._idempotent
        return False  # default: unknown operations are not idempotent

    def to_failure_record(
        self,
        *,
        operation: str = "",
        attempt: int = 0,
    ) -> FailureRecord:
        """Convert to a FailureRecord for audit/logging."""
        return FailureRecord(
            category=self.category,
            message=str(self),
            operation=operation,
            attempt=attempt,
            retryable=self.retryable,
            idempotent=self.idempotent,
            cause_type=type(self.cause).__name__ if self.cause else "",
            cause_message=str(self.cause) if self.cause else "",
            details=self.details,
            timestamp=time.monotonic(),
        )


# Typed exception subclasses (one per category)
class ValidationError(CanonicalFailure):
    category = FailureCategory.VALIDATION_ERROR


class ContractError(CanonicalFailure):
    category = FailureCategory.CONTRACT_ERROR


class NotFoundError(CanonicalFailure):
    category = FailureCategory.NOT_FOUND


class ConflictError(CanonicalFailure):
    category = FailureCategory.CONFLICT


class UnauthorizedError(CanonicalFailure):
    category = FailureCategory.UNAUTHORIZED


class ForbiddenError(CanonicalFailure):
    category = FailureCategory.FORBIDDEN


class ResourceExhaustedError(CanonicalFailure):
    category = FailureCategory.RESOURCE_EXHAUSTED


class UnavailableError(CanonicalFailure):
    category = FailureCategory.UNAVAILABLE


class TimeoutError(CanonicalFailure):
    """Timeout — deadline elapsed.  Distinct from CANCELLED."""
    category = FailureCategory.TIMEOUT


class CancelledError(CanonicalFailure):
    """Cancellation — explicit cancel request.  Distinct from TIMEOUT."""
    category = FailureCategory.CANCELLED


class PersistenceError(CanonicalFailure):
    category = FailureCategory.PERSISTENCE_ERROR


class NativeError(CanonicalFailure):
    category = FailureCategory.NATIVE_ERROR


class PlatformError(CanonicalFailure):
    category = FailureCategory.PLATFORM_ERROR


class InternalError(CanonicalFailure):
    category = FailureCategory.INTERNAL_ERROR


# Mapping from category to exception class
_CATEGORY_TO_CLASS: dict[FailureCategory, type[CanonicalFailure]] = {
    FailureCategory.VALIDATION_ERROR: ValidationError,
    FailureCategory.CONTRACT_ERROR: ContractError,
    FailureCategory.NOT_FOUND: NotFoundError,
    FailureCategory.CONFLICT: ConflictError,
    FailureCategory.UNAUTHORIZED: UnauthorizedError,
    FailureCategory.FORBIDDEN: ForbiddenError,
    FailureCategory.RESOURCE_EXHAUSTED: ResourceExhaustedError,
    FailureCategory.UNAVAILABLE: UnavailableError,
    FailureCategory.TIMEOUT: TimeoutError,
    FailureCategory.CANCELLED: CancelledError,
    FailureCategory.PERSISTENCE_ERROR: PersistenceError,
    FailureCategory.NATIVE_ERROR: NativeError,
    FailureCategory.PLATFORM_ERROR: PlatformError,
    FailureCategory.INTERNAL_ERROR: InternalError,
}


# ---------------------------------------------------------------------------
# Default retryable/idempotent per category
# ---------------------------------------------------------------------------

_DEFAULT_RETRYABLE: dict[FailureCategory, bool] = {
    FailureCategory.VALIDATION_ERROR: False,     # invalid input — fix the input
    FailureCategory.CONTRACT_ERROR: False,       # contract violation — fix the code
    FailureCategory.NOT_FOUND: False,            # resource doesn't exist
    FailureCategory.CONFLICT: True,              # may succeed on retry (version)
    FailureCategory.UNAUTHORIZED: False,         # auth failure — fix credentials
    FailureCategory.FORBIDDEN: False,            # permission denied — fix permissions
    FailureCategory.RESOURCE_EXHAUSTED: True,     # may succeed after backoff
    FailureCategory.UNAVAILABLE: True,           # may succeed after backoff
    FailureCategory.TIMEOUT: True,               # may succeed with more time
    FailureCategory.CANCELLED: False,            # never retry a cancelled operation
    FailureCategory.PERSISTENCE_ERROR: True,     # may be transient
    FailureCategory.NATIVE_ERROR: False,         # native failure — don't retry blindly
    FailureCategory.PLATFORM_ERROR: True,        # may be transient
    FailureCategory.INTERNAL_ERROR: False,       # unexpected — don't retry blindly
}


# ---------------------------------------------------------------------------
# Fallback eligibility (by error classification, not blanket)
# ---------------------------------------------------------------------------

# Categories where fallback is NEVER allowed (would hide a real problem)
_FALLBACK_FORBIDDEN: frozenset[FailureCategory] = frozenset({
    FailureCategory.VALIDATION_ERROR,    # invalid input — fix the input
    FailureCategory.CONTRACT_ERROR,      # contract violation — fix the code
    FailureCategory.TIMEOUT,             # timeout — don't hide it
    FailureCategory.CANCELLED,           # cancelled — don't hide it
    FailureCategory.UNAUTHORIZED,        # auth — don't hide it
    FailureCategory.FORBIDDEN,           # permission — don't hide it
})


def is_fallback_eligible(category: FailureCategory) -> bool:
    """Check if fallback is eligible for this error category.

    Fallback is forbidden for:
        - VALIDATION_ERROR (invalid input)
        - CONTRACT_ERROR (contract violation)
        - TIMEOUT (deadline exceeded)
        - CANCELLED (explicit cancellation)
        - UNAUTHORIZED (authentication failure)
        - FORBIDDEN (permission denied)

    Fallback is eligible for:
        - UNAVAILABLE (service down)
        - RESOURCE_EXHAUSTED (quota exhausted)
        - NATIVE_ERROR (native failure, A219 fallback)
        - PLATFORM_ERROR (platform failure)
        - PERSISTENCE_ERROR (database failure, SQLite fallback)
        - INTERNAL_ERROR (unexpected — fallback may be safer)
        - NOT_FOUND (fallback may provide alternative)
        - CONFLICT (fallback may resolve)
    """
    return category not in _FALLBACK_FORBIDDEN


# ---------------------------------------------------------------------------
# Deadline budget (single monotonic deadline, remaining budget)
# ---------------------------------------------------------------------------

@dataclass
class DeadlineBudget:
    """Single monotonic deadline with remaining budget propagation.

    All sub-operations share one original deadline.  Only the remaining
    budget is passed down — no layer creates a new full timeout.

    Cancellation propagates one-way from Python/request authority to
    native/SQL/C#.  Lower layers only observe and terminate their work.
    """
    deadline_monotonic: float  # absolute deadline (monotonic clock)
    cancelled: bool = False
    _cancel_reason: str = ""

    @classmethod
    def from_timeout(cls, timeout_s: float) -> DeadlineBudget:
        """Create from a timeout (relative to now)."""
        return cls(deadline_monotonic=time.monotonic() + timeout_s)

    @classmethod
    def from_deadline(cls, deadline: float) -> DeadlineBudget:
        """Create from an absolute monotonic deadline."""
        return cls(deadline_monotonic=deadline)

    @property
    def remaining_seconds(self) -> float:
        """Remaining time until deadline."""
        return max(0.0, self.deadline_monotonic - time.monotonic())

    @property
    def expired(self) -> bool:
        """Check if the deadline has expired."""
        return time.monotonic() >= self.deadline_monotonic

    def cancel(self, reason: str = "") -> None:
        """Request cancellation (one-way propagation)."""
        self.cancelled = True
        self._cancel_reason = reason

    @property
    def cancel_reason(self) -> str:
        return self._cancel_reason

    def check(self) -> None:
        """Raise if cancelled or expired.

        TIMEOUT and CANCELLED have distinct semantics:
            - CANCELLED: explicit cancel request
            - TIMEOUT: deadline elapsed without explicit cancel
        """
        if self.cancelled:
            raise CancelledError(
                f"operation cancelled: {self._cancel_reason}",
                category=FailureCategory.CANCELLED,
            )
        if self.expired:
            raise TimeoutError(
                "deadline exceeded",
                category=FailureCategory.TIMEOUT,
            )

    def sub_budget(self) -> DeadlineBudget:
        """Create a sub-budget with the same deadline (remaining only).

        Sub-operations get the same deadline — they don't create new
        full timeouts.  The remaining budget shrinks as time passes.
        """
        return DeadlineBudget(
            deadline_monotonic=self.deadline_monotonic,
            cancelled=self.cancelled,
        )

    def remaining_ms(self) -> int:
        """Remaining time in milliseconds (for native/SQL/C#)."""
        return int(self.remaining_seconds * 1000)


# ---------------------------------------------------------------------------
# Retry decision (Python orchestration only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryDecision:
    """Retry decision from Python orchestration.

    Retry is decided by Python only, based on:
        - retryable (error category is retryable)
        - idempotent (operation is idempotent)
        - remaining deadline (enough time for another attempt)
        - attempt budget (not exceeded)
    """
    should_retry: bool
    delay_seconds: float
    reason: str
    remaining_attempts: int


def decide_retry(
    failure: CanonicalFailure,
    *,
    attempt: int,
    max_attempts: int,
    deadline: DeadlineBudget,
    idempotent: bool = True,
    base_delay: float = 0.1,
    max_delay: float = 5.0,
) -> RetryDecision:
    """Decide whether to retry a failed operation.

    Python is the sole retry authority.  TypeScript/C/C++/C#/SQL
    never form independent retry loops.

    Rules:
        - Non-retryable errors → never retry
        - Non-idempotent operations → never retry
        - Cancelled → never retry
        - Deadline exceeded → never retry
        - Attempt budget exhausted → never retry
        - Otherwise → retry with exponential backoff
    """
    # Non-retryable category
    if not failure.retryable:
        return RetryDecision(False, 0.0, f"non-retryable: {failure.category.value}", 0)

    # Cancelled — never retry
    if failure.category == FailureCategory.CANCELLED:
        return RetryDecision(False, 0.0, "cancelled — never retry", 0)

    # Timeout — check if enough time remains
    if failure.category == FailureCategory.TIMEOUT:
        if deadline.expired:
            return RetryDecision(False, 0.0, "deadline exceeded — no time for retry", 0)

    # Non-idempotent — never retry
    if not idempotent:
        return RetryDecision(False, 0.0, "non-idempotent — never retry", 0)

    # Attempt budget exhausted
    remaining = max_attempts - attempt
    if remaining <= 0:
        return RetryDecision(False, 0.0, "attempt budget exhausted", 0)

    # Not enough time for another attempt
    if deadline.remaining_seconds < base_delay:
        return RetryDecision(False, 0.0, "insufficient remaining budget", 0)

    # Retry with exponential backoff
    delay = min(base_delay * (2.0 ** (attempt - 1)), max_delay)
    return RetryDecision(True, delay, f"retryable: {failure.category.value}", remaining)


# ---------------------------------------------------------------------------
# FailureRecord (unified audit record)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureRecord:
    """Unified failure record for audit and logging.

    Every failure that crosses a language boundary produces a
    FailureRecord.  This is the only failure representation that
    is shared across languages.
    """
    category: FailureCategory
    message: str
    operation: str
    attempt: int
    retryable: bool
    idempotent: bool
    cause_type: str       # original exception type (for debugging)
    cause_message: str     # original exception message (for debugging)
    details: dict[str, Any]
    timestamp: float       # monotonic

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "message": self.message,
            "operation": self.operation,
            "attempt": self.attempt,
            "retryable": self.retryable,
            "idempotent": self.idempotent,
            "cause_type": self.cause_type,
            "cause_message": self.cause_message,
            "details": self.details,
            "timestamp": self.timestamp,
        }

    def to_api_error(self) -> dict[str, Any]:
        """Convert to a language-neutral typed API error for TypeScript.

        TypeScript only receives this — never Python tracebacks, SQL
        errors, native status codes, or C#/.NET errors.
        """
        return {
            "error_category": self.category.value,
            "message": self.message,  # safe user-facing message
            "operation": self.operation,
            "retryable": self.retryable,
        }


# ---------------------------------------------------------------------------
# Error mapping (from raw errors to canonical taxonomy)
# ---------------------------------------------------------------------------

import re as _re

# Patterns to strip from error messages (driver/platform-specific details)
_SANITIZE_PATTERNS: list[tuple[_re.Pattern[str], str]] = [
    (_re.compile(r"psycopg\.\w+", _re.IGNORECASE), ""),
    (_re.compile(r"sqlite3\.\w+", _re.IGNORECASE), ""),
    (_re.compile(r"HRESULT:\s*0x[0-9a-fA-F]+", _re.IGNORECASE), ""),
    (_re.compile(r"std::\w+", _re.IGNORECASE), ""),
    (_re.compile(r"Win32\s+\d+", _re.IGNORECASE), ""),
]


def _sanitize_error_message(msg: str) -> str:
    """Remove driver/platform-specific details from error messages."""
    clean = msg
    for pattern, replacement in _SANITIZE_PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean.strip() or "error"


def _sanitize_native_message(msg: str) -> str:
    """Remove C++ implementation details from native error messages."""
    clean = msg
    # Strip C++ exception types, STL types, and file paths
    for pattern, replacement in _SANITIZE_PATTERNS:
        clean = pattern.sub(replacement, clean)
    # Strip C++ file paths (e.g., parser.cpp:123)
    clean = _re.sub(r"\w+\.(cpp|h|hpp|cc|c):\d+", "", clean)
    return clean.strip() or "native error"


def map_python_exception(exc: BaseException) -> CanonicalFailure:
    """Map a Python exception to a CanonicalFailure.

    This is the typed exception mapping for Python-origin errors.
    """
    # Already canonical
    if isinstance(exc, CanonicalFailure):
        return exc

    # Python built-in exceptions
    if isinstance(exc, ValueError):
        return ValidationError(str(exc), cause=exc)
    if isinstance(exc, TypeError):
        return ValidationError(str(exc), cause=exc)
    if isinstance(exc, KeyError):
        return NotFoundError(str(exc), cause=exc)
    if isinstance(exc, FileNotFoundError):
        return NotFoundError(str(exc), cause=exc)
    if isinstance(exc, PermissionError):
        return ForbiddenError(str(exc), cause=exc)
    if isinstance(exc, ConnectionError):
        return UnavailableError(str(exc), cause=exc)
    if isinstance(exc, TimeoutError):
        return TimeoutError(str(exc), cause=exc)
    if isinstance(exc, asyncio_CancelledError if _has_asyncio() else _NoAsyncio):
        return CancelledError(str(exc), cause=exc)
    if isinstance(exc, MemoryError):
        return ResourceExhaustedError(str(exc), cause=exc)
    if isinstance(exc, NotImplementedError):
        return ContractError(str(exc), cause=exc)
    if isinstance(exc, RuntimeError):
        return InternalError(str(exc), cause=exc)

    # Default
    return InternalError(str(exc), cause=exc)


def _has_asyncio() -> bool:
    try:
        import asyncio
        return True
    except ImportError:
        return False


if _has_asyncio():
    import asyncio as _asyncio
    asyncio_CancelledError = _asyncio.CancelledError
else:
    asyncio_CancelledError = type("_NoAsyncio", (), {})  # placeholder


class _NoAsyncio:
    pass


def map_sql_error(exc: BaseException) -> CanonicalFailure:
    """Map a SQL driver error to a CanonicalFailure.

    SQL driver errors (psycopg, sqlite3) are mapped to the canonical
    taxonomy.  Raw SQL errors never cross the language boundary —
    the message is sanitized to remove driver-specific details.
    """
    exc_name = type(exc).__name__
    exc_msg = str(exc).casefold()

    # Sanitize: remove driver-specific prefixes and details
    clean_msg = _sanitize_error_message(str(exc))

    # psycopg errors
    if "psycopg" in exc_name.lower() or "operationalerror" in exc_name.lower():
        if "connection" in exc_msg or "connect" in exc_msg:
            return UnavailableError("database unavailable", cause=exc)
        if "timeout" in exc_msg or "timed out" in exc_msg:
            return TimeoutError("database operation timed out", cause=exc)
        if "permission" in exc_msg or "denied" in exc_msg:
            return ForbiddenError("database permission denied", cause=exc)
        if "deadlock" in exc_msg:
            return ConflictError("database deadlock", cause=exc, retryable=True)
        if "serialization" in exc_msg:
            return ConflictError("serialization conflict", cause=exc, retryable=True)
        if "duplicate" in exc_msg or "already exists" in exc_msg:
            return ConflictError("duplicate resource", cause=exc, retryable=False)
        if "not found" in exc_msg or "does not exist" in exc_msg:
            return NotFoundError("database resource not found", cause=exc)
        return PersistenceError("database error", cause=exc)

    # sqlite3 errors
    if "sqlite3" in exc_name.lower() or "database" in exc_name.lower():
        if "locked" in exc_msg or "busy" in exc_msg:
            return UnavailableError("database locked", cause=exc, retryable=True)
        if "disk" in exc_msg or "io" in exc_msg or "corrupt" in exc_msg:
            return PersistenceError("database I/O error", cause=exc, retryable=False)
        if "constraint" in exc_msg:
            return ConflictError("constraint violation", cause=exc, retryable=False)
        return PersistenceError("database error", cause=exc)

    # Generic DB error
    return PersistenceError("database error", cause=exc)


def map_native_status(
    status: int,
    message: str = "",
) -> CanonicalFailure:
    """Map a native status code to a CanonicalFailure.

    C++ exceptions are caught at the C ABI and mapped to stable status
    codes.  No exception/RTTI/STL/HRESULT leaks across the boundary.
    The message is sanitized to remove C++ implementation details.
    """
    try:
        native_status = NativeStatus(status)
    except ValueError:
        native_status = NativeStatus.UNKNOWN

    category = _NATIVE_STATUS_TO_CATEGORY.get(native_status, FailureCategory.INTERNAL_ERROR)
    cls = _CATEGORY_TO_CLASS[category]
    # Sanitize: remove any C++ implementation details from the message
    clean_msg = _sanitize_native_message(message) if message else f"native status {status}"
    return cls(clean_msg, cause=None)


def map_platform_error(exc: BaseException) -> CanonicalFailure:
    """Map a C#/.NET/Win32 platform error to a CanonicalFailure.

    Platform errors (HRESULT, Win32, .NET exceptions) are mapped to
    the canonical taxonomy.  Raw platform errors never cross the
    language boundary — the message is sanitized.
    """
    exc_msg = str(exc).casefold()

    if "timeout" in exc_msg or "timed out" in exc_msg:
        return TimeoutError("platform operation timed out", cause=exc)
    if "cancel" in exc_msg:
        return CancelledError("platform operation cancelled", cause=exc)
    if "access" in exc_msg or "denied" in exc_msg or "permission" in exc_msg:
        return ForbiddenError("platform access denied", cause=exc)
    if "not found" in exc_msg or "not exist" in exc_msg:
        return NotFoundError("platform resource not found", cause=exc)
    if "out of memory" in exc_msg or "memory" in exc_msg:
        return ResourceExhaustedError("platform memory exhausted", cause=exc)
    if "argument" in exc_msg or "invalid" in exc_msg:
        return ValidationError("invalid platform argument", cause=exc)

    return PlatformError("platform error", cause=exc)


def map_any_error(exc: BaseException) -> CanonicalFailure:
    """Map any error to a CanonicalFailure.

    This is the universal entry point.  It tries specific mappers
    based on the error type and falls back to InternalError.
    """
    # Already canonical
    if isinstance(exc, CanonicalFailure):
        return exc

    exc_name = type(exc).__name__

    # SQL driver errors
    if any(marker in exc_name.lower() for marker in ("psycopg", "sqlite", "database", "operational")):
        return map_sql_error(exc)

    # Platform errors (C#/.NET/Win32)
    if any(marker in exc_name.lower() for marker in ("hresult", "win32", "platform", "com")):
        return map_platform_error(exc)

    # Python built-in exceptions
    return map_python_exception(exc)


# ---------------------------------------------------------------------------
# Fault injection (for testing)
# ---------------------------------------------------------------------------

@dataclass
class FaultInjector:
    """Fault injection for testing error mapping, retry, fallback.

    Injects failures at specific points to verify:
        - Error mapping (raw → canonical)
        - Retry/fallback eligibility
        - Deadline/cancellation propagation
        - Resource cleanup
        - Audit and user-facing result
    """
    _faults: dict[str, BaseException] = field(default_factory=dict)
    _injected: list[str] = field(default_factory=list)

    def inject(self, point: str, error: BaseException) -> None:
        """Inject a fault at a named point."""
        self._faults[point] = error

    def check(self, point: str) -> None:
        """Check if a fault should fire at this point."""
        if point in self._faults:
            self._injected.append(point)
            raise self._faults[point]

    def was_injected(self, point: str) -> bool:
        """Check if a fault was injected at this point."""
        return point in self._injected

    @property
    def injected_count(self) -> int:
        return len(self._injected)

    def clear(self) -> None:
        self._faults.clear()
        self._injected.clear()


__all__ = [
    "FAILURE_TAXONOMY_VERSION",
    "FailureCategory",
    "ALL_CATEGORIES",
    "NativeStatus",
    "CanonicalFailure",
    "ValidationError",
    "ContractError",
    "NotFoundError",
    "ConflictError",
    "UnauthorizedError",
    "ForbiddenError",
    "ResourceExhaustedError",
    "UnavailableError",
    "TimeoutError",
    "CancelledError",
    "PersistenceError",
    "NativeError",
    "PlatformError",
    "InternalError",
    "is_fallback_eligible",
    "DeadlineBudget",
    "RetryDecision",
    "decide_retry",
    "FailureRecord",
    "map_python_exception",
    "map_sql_error",
    "map_native_status",
    "map_platform_error",
    "map_any_error",
    "FaultInjector",
]
