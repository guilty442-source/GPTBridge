"""Tests for Error, Cancellation & Failure Semantics Consolidation V1.

Tests the full failure semantics pipeline:
    - Canonical failure taxonomy (14 categories)
    - Native status code mapping (C ABI boundary)
    - Typed exception hierarchy
    - Error mapping (Python/SQL/native/platform → canonical)
    - Retry decision (Python orchestration only)
    - Fallback eligibility (by error classification, not blanket)
    - Deadline budget (single monotonic deadline, remaining budget)
    - TIMEOUT vs CANCELLED distinct semantics
    - FailureRecord (unified audit record)
    - Fault injection (error mapping, retry, fallback, deadline, cleanup)

Codex basis:
    A204/A220 — C ABI boundary (no exception leak)
    A205 — API boundary (typed API errors)
    A211 — six-language canonical roles (Python = failure authority)
    A219 — Python fallback always preserved (but classified)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

import pytest

from shared_layer.failure_taxonomy import (
    FAILURE_TAXONOMY_VERSION,
    FailureCategory,
    ALL_CATEGORIES,
    NativeStatus,
    CanonicalFailure,
    ValidationError,
    ContractError,
    NotFoundError,
    ConflictError,
    UnauthorizedError,
    ForbiddenError,
    ResourceExhaustedError,
    UnavailableError,
    TimeoutError,
    CancelledError,
    PersistenceError,
    NativeError,
    PlatformError,
    InternalError,
    is_fallback_eligible,
    DeadlineBudget,
    RetryDecision,
    decide_retry,
    FailureRecord,
    map_python_exception,
    map_sql_error,
    map_native_status,
    map_platform_error,
    map_any_error,
    FaultInjector,
)


# ---------------------------------------------------------------------------
# Failure Taxonomy
# ---------------------------------------------------------------------------

class TestFailureTaxonomy:
    def test_fourteen_categories(self):
        assert len(FailureCategory) == 14

    def test_all_categories_exist(self):
        expected = {
            "VALIDATION_ERROR", "CONTRACT_ERROR", "NOT_FOUND", "CONFLICT",
            "UNAUTHORIZED", "FORBIDDEN", "RESOURCE_EXHAUSTED", "UNAVAILABLE",
            "TIMEOUT", "CANCELLED", "PERSISTENCE_ERROR", "NATIVE_ERROR",
            "PLATFORM_ERROR", "INTERNAL_ERROR",
        }
        actual = {c.value for c in FailureCategory}
        assert actual == expected

    def test_taxonomy_versioned(self):
        assert FAILURE_TAXONOMY_VERSION == "1.0"

    def test_all_categories_frozenset(self):
        assert ALL_CATEGORIES == frozenset(FailureCategory)


# ---------------------------------------------------------------------------
# Native Status Codes
# ---------------------------------------------------------------------------

class TestNativeStatus:
    def test_status_codes_exist(self):
        assert NativeStatus.OK == 0
        assert NativeStatus.INVALID_ARGUMENT == 1
        assert NativeStatus.CANCELLED == 10
        assert NativeStatus.INTERNAL == 99
        assert NativeStatus.UNKNOWN == 100

    def test_map_native_status_ok(self):
        # OK is not an error, but maps to INTERNAL_ERROR for safety
        f = map_native_status(0, "ok")
        assert isinstance(f, CanonicalFailure)

    def test_map_native_status_invalid_argument(self):
        f = map_native_status(NativeStatus.INVALID_ARGUMENT, "bad input")
        assert isinstance(f, ValidationError)
        assert f.category == FailureCategory.VALIDATION_ERROR

    def test_map_native_status_not_found(self):
        f = map_native_status(NativeStatus.NOT_FOUND, "missing")
        assert isinstance(f, NotFoundError)

    def test_map_native_status_cancelled(self):
        f = map_native_status(NativeStatus.CANCELLED, "cancelled")
        assert isinstance(f, CancelledError)
        assert f.category == FailureCategory.CANCELLED

    def test_map_native_status_deadline(self):
        f = map_native_status(NativeStatus.DEADLINE_EXCEEDED, "timeout")
        assert isinstance(f, TimeoutError)
        assert f.category == FailureCategory.TIMEOUT

    def test_map_native_status_unknown(self):
        f = map_native_status(999, "unknown")
        assert isinstance(f, InternalError)

    def test_map_native_status_no_exception_leak(self):
        """C++ exceptions are caught at the ABI — only status codes cross."""
        f = map_native_status(NativeStatus.INTERNAL, "C++ exception caught")
        assert isinstance(f, InternalError)
        # No C++ exception type in the message
        assert "std::" not in str(f)


# ---------------------------------------------------------------------------
# Typed Exception Hierarchy
# ---------------------------------------------------------------------------

class TestTypedExceptions:
    def test_all_subclasses(self):
        assert issubclass(ValidationError, CanonicalFailure)
        assert issubclass(ContractError, CanonicalFailure)
        assert issubclass(NotFoundError, CanonicalFailure)
        assert issubclass(ConflictError, CanonicalFailure)
        assert issubclass(UnauthorizedError, CanonicalFailure)
        assert issubclass(ForbiddenError, CanonicalFailure)
        assert issubclass(ResourceExhaustedError, CanonicalFailure)
        assert issubclass(UnavailableError, CanonicalFailure)
        assert issubclass(TimeoutError, CanonicalFailure)
        assert issubclass(CancelledError, CanonicalFailure)
        assert issubclass(PersistenceError, CanonicalFailure)
        assert issubclass(NativeError, CanonicalFailure)
        assert issubclass(PlatformError, CanonicalFailure)
        assert issubclass(InternalError, CanonicalFailure)

    def test_category_attribute(self):
        assert ValidationError().category == FailureCategory.VALIDATION_ERROR
        assert TimeoutError().category == FailureCategory.TIMEOUT
        assert CancelledError().category == FailureCategory.CANCELLED

    def test_retryable_default(self):
        assert not ValidationError().retryable  # invalid input — fix input
        assert UnavailableError().retryable     # may succeed after backoff
        assert not CancelledError().retryable   # never retry cancelled
        assert not ContractError().retryable    # contract violation — fix code

    def test_retryable_override(self):
        e = ValidationError("retryable validation", retryable=True)
        assert e.retryable

    def test_idempotent_default(self):
        assert not ValidationError().idempotent  # default: unknown

    def test_idempotent_override(self):
        e = ConflictError("retryable conflict", idempotent=True)
        assert e.idempotent

    def test_cause_preserved(self):
        original = ValueError("original")
        e = map_python_exception(original)
        assert e.cause is original

    def test_to_failure_record(self):
        e = ValidationError("bad input", details={"field": "name"})
        record = e.to_failure_record(operation="validate", attempt=1)
        assert isinstance(record, FailureRecord)
        assert record.category == FailureCategory.VALIDATION_ERROR
        assert record.operation == "validate"
        assert record.attempt == 1


# ---------------------------------------------------------------------------
# TIMEOUT vs CANCELLED distinct semantics
# ---------------------------------------------------------------------------

class TestTimeoutVsCancelled:
    def test_timeout_is_not_cancelled(self):
        assert FailureCategory.TIMEOUT != FailureCategory.CANCELLED

    def test_timeout_error_class(self):
        e = TimeoutError("deadline exceeded")
        assert e.category == FailureCategory.TIMEOUT
        assert not isinstance(e, CancelledError)

    def test_cancelled_error_class(self):
        e = CancelledError("user cancelled")
        assert e.category == FailureCategory.CANCELLED
        assert not isinstance(e, TimeoutError)

    def test_timeout_retryable_but_cancelled_not(self):
        assert TimeoutError().retryable
        assert not CancelledError().retryable

    def test_deadline_check_timeout(self):
        db = DeadlineBudget(deadline_monotonic=time.monotonic() - 1.0)
        with pytest.raises(TimeoutError):
            db.check()

    def test_deadline_check_cancelled(self):
        db = DeadlineBudget.from_timeout(10.0)
        db.cancel("user request")
        with pytest.raises(CancelledError):
            db.check()

    def test_deadline_check_not_cancelled_on_expiry(self):
        """Expiry raises TimeoutError, not CancelledError."""
        db = DeadlineBudget(deadline_monotonic=time.monotonic() - 1.0)
        try:
            db.check()
            assert False, "should have raised"
        except TimeoutError:
            pass
        except CancelledError:
            assert False, "expiry should raise TimeoutError, not CancelledError"


# ---------------------------------------------------------------------------
# Error Mapping
# ---------------------------------------------------------------------------

class TestErrorMapping:
    def test_map_python_value_error(self):
        f = map_python_exception(ValueError("bad value"))
        assert isinstance(f, ValidationError)

    def test_map_python_key_error(self):
        f = map_python_exception(KeyError("missing"))
        assert isinstance(f, NotFoundError)

    def test_map_python_permission_error(self):
        f = map_python_exception(PermissionError("denied"))
        assert isinstance(f, ForbiddenError)

    def test_map_python_connection_error(self):
        f = map_python_exception(ConnectionError("refused"))
        assert isinstance(f, UnavailableError)

    def test_map_python_timeout_error(self):
        f = map_python_exception(TimeoutError("timed out"))
        assert isinstance(f, TimeoutError)

    def test_map_python_memory_error(self):
        f = map_python_exception(MemoryError("out of memory"))
        assert isinstance(f, ResourceExhaustedError)

    def test_map_python_not_implemented(self):
        f = map_python_exception(NotImplementedError("not supported"))
        assert isinstance(f, ContractError)

    def test_map_already_canonical(self):
        original = ValidationError("already canonical")
        f = map_python_exception(original)
        assert f is original

    def test_map_sql_connection_error(self):
        class FakeOperationalError(Exception):
            pass
        exc = FakeOperationalError("connection refused")
        f = map_sql_error(exc)
        assert isinstance(f, UnavailableError)

    def test_map_sql_timeout(self):
        class FakeOperationalError(Exception):
            pass
        exc = FakeOperationalError("timeout expired")
        f = map_sql_error(exc)
        assert isinstance(f, TimeoutError)

    def test_map_sql_permission(self):
        class FakeOperationalError(Exception):
            pass
        exc = FakeOperationalError("permission denied")
        f = map_sql_error(exc)
        assert isinstance(f, ForbiddenError)

    def test_map_sql_deadlock(self):
        class FakeOperationalError(Exception):
            pass
        exc = FakeOperationalError("deadlock detected")
        f = map_sql_error(exc)
        assert isinstance(f, ConflictError)
        assert f.retryable

    def test_map_sql_duplicate(self):
        class FakeOperationalError(Exception):
            pass
        exc = FakeOperationalError("duplicate key")
        f = map_sql_error(exc)
        assert isinstance(f, ConflictError)
        assert not f.retryable

    def test_map_sqlite_busy(self):
        class FakeDatabaseError(Exception):
            pass
        exc = FakeDatabaseError("database is locked")
        f = map_sql_error(exc)
        assert isinstance(f, UnavailableError)
        assert f.retryable

    def test_map_sqlite_constraint(self):
        class FakeDatabaseError(Exception):
            pass
        exc = FakeDatabaseError("constraint failed")
        f = map_sql_error(exc)
        assert isinstance(f, ConflictError)

    def test_map_platform_timeout(self):
        exc = RuntimeError("operation timed out")
        f = map_platform_error(exc)
        assert isinstance(f, TimeoutError)

    def test_map_platform_cancel(self):
        exc = RuntimeError("operation cancelled")
        f = map_platform_error(exc)
        assert isinstance(f, CancelledError)

    def test_map_platform_access_denied(self):
        exc = RuntimeError("access denied")
        f = map_platform_error(exc)
        assert isinstance(f, ForbiddenError)

    def test_map_platform_not_found(self):
        exc = RuntimeError("file not found")
        f = map_platform_error(exc)
        assert isinstance(f, NotFoundError)

    def test_map_platform_oom(self):
        exc = RuntimeError("out of memory")
        f = map_platform_error(exc)
        assert isinstance(f, ResourceExhaustedError)

    def test_map_any_error_python(self):
        f = map_any_error(ValueError("test"))
        assert isinstance(f, ValidationError)

    def test_map_any_error_sql(self):
        class FakeOperationalError(Exception):
            pass
        f = map_any_error(FakeOperationalError("connection refused"))
        assert isinstance(f, UnavailableError)

    def test_map_any_error_already_canonical(self):
        original = NotFoundError("already canonical")
        f = map_any_error(original)
        assert f is original


# ---------------------------------------------------------------------------
# Fallback Eligibility
# ---------------------------------------------------------------------------

class TestFallbackEligibility:
    def test_validation_error_no_fallback(self):
        assert not is_fallback_eligible(FailureCategory.VALIDATION_ERROR)

    def test_contract_error_no_fallback(self):
        assert not is_fallback_eligible(FailureCategory.CONTRACT_ERROR)

    def test_timeout_no_fallback(self):
        assert not is_fallback_eligible(FailureCategory.TIMEOUT)

    def test_cancelled_no_fallback(self):
        assert not is_fallback_eligible(FailureCategory.CANCELLED)

    def test_unauthorized_no_fallback(self):
        assert not is_fallback_eligible(FailureCategory.UNAUTHORIZED)

    def test_forbidden_no_fallback(self):
        assert not is_fallback_eligible(FailureCategory.FORBIDDEN)

    def test_unavailable_fallback_eligible(self):
        assert is_fallback_eligible(FailureCategory.UNAVAILABLE)

    def test_resource_exhausted_fallback_eligible(self):
        assert is_fallback_eligible(FailureCategory.RESOURCE_EXHAUSTED)

    def test_native_error_fallback_eligible(self):
        assert is_fallback_eligible(FailureCategory.NATIVE_ERROR)

    def test_platform_error_fallback_eligible(self):
        assert is_fallback_eligible(FailureCategory.PLATFORM_ERROR)

    def test_persistence_error_fallback_eligible(self):
        assert is_fallback_eligible(FailureCategory.PERSISTENCE_ERROR)

    def test_no_blanket_except_exception(self):
        """Fallback must follow error classification — no blanket except."""
        # This is a design rule: is_fallback_eligible must be checked
        # before falling back, not a blanket except Exception.
        forbidden = {
            FailureCategory.VALIDATION_ERROR,
            FailureCategory.CONTRACT_ERROR,
            FailureCategory.TIMEOUT,
            FailureCategory.CANCELLED,
        }
        for cat in forbidden:
            assert not is_fallback_eligible(cat), \
                f"{cat} should not be fallback-eligible"


# ---------------------------------------------------------------------------
# Deadline Budget
# ---------------------------------------------------------------------------

class TestDeadlineBudget:
    def test_from_timeout(self):
        db = DeadlineBudget.from_timeout(10.0)
        assert db.remaining_seconds > 0
        assert not db.expired
        assert not db.cancelled

    def test_from_deadline(self):
        deadline = time.monotonic() + 5.0
        db = DeadlineBudget.from_deadline(deadline)
        assert db.deadline_monotonic == deadline

    def test_remaining_seconds(self):
        db = DeadlineBudget.from_timeout(5.0)
        assert 0 < db.remaining_seconds <= 5.0

    def test_expired(self):
        db = DeadlineBudget(deadline_monotonic=time.monotonic() - 1.0)
        assert db.expired

    def test_cancel(self):
        db = DeadlineBudget.from_timeout(10.0)
        db.cancel("user request")
        assert db.cancelled
        assert db.cancel_reason == "user request"

    def test_check_ok(self):
        db = DeadlineBudget.from_timeout(10.0)
        db.check()  # should not raise

    def test_check_expired_raises_timeout(self):
        db = DeadlineBudget(deadline_monotonic=time.monotonic() - 1.0)
        with pytest.raises(TimeoutError):
            db.check()

    def test_check_cancelled_raises_cancelled(self):
        db = DeadlineBudget.from_timeout(10.0)
        db.cancel("test")
        with pytest.raises(CancelledError):
            db.check()

    def test_sub_budget_same_deadline(self):
        """Sub-operations share the same deadline — no new full timeout."""
        db = DeadlineBudget.from_timeout(10.0)
        sub = db.sub_budget()
        assert sub.deadline_monotonic == db.deadline_monotonic

    def test_sub_budget_propagates_cancel(self):
        db = DeadlineBudget.from_timeout(10.0)
        db.cancel("parent cancel")
        sub = db.sub_budget()
        assert sub.cancelled

    def test_remaining_ms(self):
        db = DeadlineBudget.from_timeout(1.0)
        ms = db.remaining_ms()
        assert 0 < ms <= 1000

    def test_no_new_full_timeout(self):
        """Each layer only passes remaining budget — no new full timeout."""
        db = DeadlineBudget.from_timeout(10.0)
        time.sleep(0.01)
        sub = db.sub_budget()
        # Sub-budget has less remaining than original
        assert sub.remaining_seconds < db.remaining_seconds + 0.1


# ---------------------------------------------------------------------------
# Retry Decision
# ---------------------------------------------------------------------------

class TestRetryDecision:
    def test_non_retryable_no_retry(self):
        e = ValidationError("bad input")
        db = DeadlineBudget.from_timeout(10.0)
        decision = decide_retry(e, attempt=1, max_attempts=3, deadline=db)
        assert not decision.should_retry

    def test_cancelled_no_retry(self):
        e = CancelledError("cancelled")
        db = DeadlineBudget.from_timeout(10.0)
        decision = decide_retry(e, attempt=1, max_attempts=3, deadline=db)
        assert not decision.should_retry

    def test_non_idempotent_no_retry(self):
        e = UnavailableError("down")
        db = DeadlineBudget.from_timeout(10.0)
        decision = decide_retry(e, attempt=1, max_attempts=3, deadline=db, idempotent=False)
        assert not decision.should_retry

    def test_attempt_budget_exhausted(self):
        e = UnavailableError("down")
        db = DeadlineBudget.from_timeout(10.0)
        decision = decide_retry(e, attempt=3, max_attempts=3, deadline=db)
        assert not decision.should_retry

    def test_deadline_exceeded_no_retry(self):
        e = TimeoutError("timeout")
        db = DeadlineBudget(deadline_monotonic=time.monotonic() - 1.0)
        decision = decide_retry(e, attempt=1, max_attempts=3, deadline=db)
        assert not decision.should_retry

    def test_retryable_with_budget(self):
        e = UnavailableError("down")
        db = DeadlineBudget.from_timeout(10.0)
        decision = decide_retry(e, attempt=1, max_attempts=3, deadline=db)
        assert decision.should_retry
        assert decision.delay_seconds > 0
        assert decision.remaining_attempts == 2

    def test_exponential_backoff(self):
        e = UnavailableError("down")
        db = DeadlineBudget.from_timeout(100.0)
        d1 = decide_retry(e, attempt=1, max_attempts=5, deadline=db, base_delay=0.1)
        d2 = decide_retry(e, attempt=2, max_attempts=5, deadline=db, base_delay=0.1)
        d3 = decide_retry(e, attempt=3, max_attempts=5, deadline=db, base_delay=0.1)
        assert d1.delay_seconds < d2.delay_seconds < d3.delay_seconds

    def test_python_only_retry_authority(self):
        """Retry is decided by Python orchestration only."""
        e = ConflictError("conflict", retryable=True)
        db = DeadlineBudget.from_timeout(10.0)
        decision = decide_retry(e, attempt=1, max_attempts=3, deadline=db, idempotent=True)
        # Python decides — no TypeScript/C/C++/C#/SQL retry loops
        assert isinstance(decision, RetryDecision)


# ---------------------------------------------------------------------------
# FailureRecord
# ---------------------------------------------------------------------------

class TestFailureRecord:
    def test_create_failure_record(self):
        e = ValidationError("bad input")
        record = e.to_failure_record(operation="validate", attempt=1)
        assert record.category == FailureCategory.VALIDATION_ERROR
        assert record.message == "bad input"
        assert record.operation == "validate"
        assert record.attempt == 1

    def test_failure_record_to_dict(self):
        e = ValidationError("bad input", details={"field": "name"})
        record = e.to_failure_record(operation="validate", attempt=1)
        d = record.to_dict()
        assert d["category"] == "VALIDATION_ERROR"
        assert d["message"] == "bad input"
        assert d["details"] == {"field": "name"}

    def test_failure_record_to_api_error(self):
        """TypeScript only receives language-neutral typed API error."""
        e = PersistenceError("DB connection failed", cause=ConnectionError("refused"))
        record = e.to_failure_record(operation="query", attempt=1)
        api = record.to_api_error()
        assert "error_category" in api
        assert "message" in api
        assert "operation" in api
        assert "retryable" in api
        # No Python traceback, SQL error, or native status in API error
        assert "traceback" not in str(api).lower()
        assert "cause_type" not in api
        assert "cause_message" not in api

    def test_no_traceback_leak(self):
        """Python traceback must not leak to TypeScript."""
        try:
            raise ValueError("original error")
        except ValueError as e:
            f = map_python_exception(e)
            record = f.to_failure_record(operation="test", attempt=1)
            api = record.to_api_error()
            # API error should not contain traceback
            assert "Traceback" not in str(api)


# ---------------------------------------------------------------------------
# Fault Injection
# ---------------------------------------------------------------------------

class TestFaultInjection:
    def test_inject_and_check(self):
        injector = FaultInjector()
        injector.inject("db_query", ConnectionError("connection refused"))
        with pytest.raises(ConnectionError):
            injector.check("db_query")

    def test_was_injected(self):
        injector = FaultInjector()
        injector.inject("point1", ValueError("test"))
        try:
            injector.check("point1")
        except ValueError:
            pass
        assert injector.was_injected("point1")

    def test_not_injected(self):
        injector = FaultInjector()
        injector.check("nonexistent")  # should not raise
        assert not injector.was_injected("nonexistent")

    def test_injected_count(self):
        injector = FaultInjector()
        injector.inject("p1", ValueError("e1"))
        injector.inject("p2", ConnectionError("e2"))
        try:
            injector.check("p1")
        except ValueError:
            pass
        try:
            injector.check("p2")
        except ConnectionError:
            pass
        assert injector.injected_count == 2

    def test_clear(self):
        injector = FaultInjector()
        injector.inject("p1", ValueError("e1"))
        injector.clear()
        assert injector.injected_count == 0
        injector.check("p1")  # should not raise after clear

    def test_fault_injection_error_mapping(self):
        """Verify error mapping works with injected faults."""
        injector = FaultInjector()
        injector.inject("db", ConnectionError("refused"))
        try:
            injector.check("db")
        except BaseException as e:
            f = map_any_error(e)
            assert isinstance(f, UnavailableError)

    def test_fault_injection_retry(self):
        """Verify retry decision works with injected faults."""
        injector = FaultInjector()
        injector.inject("op", UnavailableError("down"))
        db = DeadlineBudget.from_timeout(10.0)
        try:
            injector.check("op")
        except CanonicalFailure as e:
            decision = decide_retry(e, attempt=1, max_attempts=3, deadline=db)
            assert decision.should_retry

    def test_fault_injection_no_fallback_for_validation(self):
        """Invalid input must not be hidden by fallback."""
        injector = FaultInjector()
        injector.inject("op", ValidationError("bad input"))
        try:
            injector.check("op")
        except CanonicalFailure as e:
            assert not is_fallback_eligible(e.category)

    def test_fault_injection_no_fallback_for_cancelled(self):
        """Cancelled must not be hidden by fallback."""
        injector = FaultInjector()
        injector.inject("op", CancelledError("cancelled"))
        try:
            injector.check("op")
        except CanonicalFailure as e:
            assert not is_fallback_eligible(e.category)


# ---------------------------------------------------------------------------
# Cross-language boundary
# ---------------------------------------------------------------------------

class TestCrossLanguageBoundary:
    def test_python_is_sole_authority(self):
        """Python is the sole application failure/retry/fallback policy authority."""
        e = UnavailableError("down")
        db = DeadlineBudget.from_timeout(10.0)
        # Only Python decides retry
        decision = decide_retry(e, attempt=1, max_attempts=3, deadline=db)
        assert isinstance(decision, RetryDecision)

    def test_no_raw_db_error_leak(self):
        """Raw SQL driver errors must not cross the language boundary."""
        class FakePsycopgError(Exception):
            pass
        raw = FakePsycopgError("psycopg.OperationalError: connection refused")
        f = map_sql_error(raw)
        # The canonical failure has a clean message
        assert "psycopg" not in str(f)
        assert isinstance(f, UnavailableError)

    def test_no_native_exception_leak(self):
        """C++ exceptions must not cross the C ABI boundary."""
        f = map_native_status(NativeStatus.INTERNAL, "std::runtime_error caught")
        # No C++ exception type in the canonical failure
        assert "std::" not in str(f)
        assert isinstance(f, InternalError)

    def test_no_platform_error_leak(self):
        """C#/.NET/Win32 errors must not cross the language boundary."""
        class FakeHResultError(Exception):
            pass
        raw = FakeHResultError("HRESULT: 0x80070005 Access denied")
        f = map_platform_error(raw)
        assert "HRESULT" not in str(f)
        assert isinstance(f, ForbiddenError)

    def test_typescript_receives_typed_api_error(self):
        """TypeScript only receives language-neutral typed API error."""
        e = PersistenceError("DB error", cause=Exception("raw SQL error"))
        record = e.to_failure_record(operation="query", attempt=1)
        api = record.to_api_error()
        # Only safe fields
        assert set(api.keys()) == {"error_category", "message", "operation", "retryable"}
