"""Tests for Cross-Language Test Matrix Closure V1.

Tests the full test matrix pipeline:
    - Test frameworks (A210: pytest/vitest/googletest/ctest/xunit/sql)
    - Test categories (unit/executor/boundary_contract/fallback/failure/integration)
    - Boundary types (TS↔Python, Python↔SQL, Python↔pybind11, pybind11↔C ABI, C ABI↔C++, C ABI↔C#)
    - Canonical type fixtures (min/max, int64, null/missing, UTF-8, enum unknown, timestamp/duration, buffer bounds)
    - Test tiers (TEST_FAST/TEST_STANDARD/TEST_FULL)
    - Flaky test policy (no auto-retry, quarantine, block release)
    - Capability Test Coverage Record
    - Native/fallback parity (same semantic corpus)
    - Fault injection coverage (9 types)
    - Test path validation (public/boundary path only)
    - Integration owned by Python pytest only

Codex basis:
    A210 — six-language-test-boundary
    A208 — release verification
    A219 — Python fallback always preserved
    A220 — C ABI boundary
"""
from __future__ import annotations

import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

import pytest

from shared_layer.test_matrix_consolidation import (
    TEST_MATRIX_VERSION,
    TestFramework,
    LANGUAGE_FRAMEWORK,
    TestCategory,
    REQUIRED_TEST_CATEGORIES,
    BoundaryType,
    ALL_BOUNDARIES,
    TypeFixture,
    REQUIRED_TYPE_FIXTURES,
    TestTier,
    TIER_CONTENT,
    TIER_ENVIRONMENTS,
    select_tier_by_env,
    FlakyPolicy,
    CRITICAL_TEST_CATEGORIES,
    FlakyTestRecord,
    classify_flaky,
    TestEntry,
    CapabilityTestCoverage,
    TestMatrixRegistry,
    NativeFallbackParityTest,
    verify_parity,
    FaultInjectionType,
    REQUIRED_FAULT_INJECTION,
    FaultInjectionCoverage,
    FaultInjectionRegistry,
    validate_test_path,
    validate_integration_owned_by_python,
    TestMatrixResult,
    run_test_matrix,
)


# ---------------------------------------------------------------------------
# Test Frameworks (A210)
# ---------------------------------------------------------------------------

class TestFrameworks:
    def test_six_frameworks(self):
        assert len(TestFramework) == 6

    def test_python_uses_pytest(self):
        assert LANGUAGE_FRAMEWORK["python"] == TestFramework.PYTEST

    def test_typescript_uses_vitest(self):
        assert LANGUAGE_FRAMEWORK["typescript"] == TestFramework.VITEST

    def test_cpp_uses_googletest(self):
        assert LANGUAGE_FRAMEWORK["cpp"] == TestFramework.GOTEST

    def test_c_uses_ctest(self):
        assert LANGUAGE_FRAMEWORK["c"] == TestFramework.CTEST

    def test_csharp_uses_xunit(self):
        assert LANGUAGE_FRAMEWORK["csharp"] == TestFramework.XUNIT

    def test_sql_uses_database_test(self):
        assert LANGUAGE_FRAMEWORK["sql"] == TestFramework.SQL_TEST


# ---------------------------------------------------------------------------
# Test Categories
# ---------------------------------------------------------------------------

class TestCategories:
    def test_six_categories(self):
        assert len(TestCategory) == 6
        assert TestCategory.UNIT.value == "unit"
        assert TestCategory.EXECUTOR.value == "executor"
        assert TestCategory.BOUNDARY_CONTRACT.value == "boundary_contract"
        assert TestCategory.FALLBACK.value == "fallback"
        assert TestCategory.FAILURE.value == "failure"
        assert TestCategory.INTEGRATION.value == "integration"

    def test_required_categories(self):
        assert REQUIRED_TEST_CATEGORIES == frozenset(TestCategory)


# ---------------------------------------------------------------------------
# Boundary Types
# ---------------------------------------------------------------------------

class TestBoundaryTypes:
    def test_six_boundaries(self):
        assert len(BoundaryType) == 6

    def test_all_boundaries(self):
        expected = {
            "typescript_python", "python_sql", "python_pybind11",
            "pybind11_c_abi", "c_abi_cpp", "c_abi_csharp",
        }
        actual = {b.value for b in BoundaryType}
        assert actual == expected

    def test_typescript_python_boundary(self):
        assert BoundaryType.TYPESCRIPT_PYTHON in ALL_BOUNDARIES

    def test_python_sql_boundary(self):
        assert BoundaryType.PYTHON_SQL in ALL_BOUNDARIES

    def test_python_pybind11_boundary(self):
        assert BoundaryType.PYTHON_PYBIND11 in ALL_BOUNDARIES

    def test_pybind11_c_abi_boundary(self):
        assert BoundaryType.PYBIND11_C_ABI in ALL_BOUNDARIES

    def test_c_abi_cpp_boundary(self):
        assert BoundaryType.C_ABI_CPP in ALL_BOUNDARIES

    def test_c_abi_csharp_boundary(self):
        assert BoundaryType.C_ABI_CSHARP in ALL_BOUNDARIES


# ---------------------------------------------------------------------------
# Canonical Type Fixtures
# ---------------------------------------------------------------------------

class TestTypeFixtures:
    def test_required_fixtures(self):
        expected = {
            "min_value", "max_value", "int64", "null", "missing",
            "utf8", "enum_unknown", "timestamp", "duration", "buffer_bounds",
        }
        actual = {f.value for f in TypeFixture}
        assert actual == expected

    def test_min_max(self):
        assert TypeFixture.MIN_VALUE in REQUIRED_TYPE_FIXTURES
        assert TypeFixture.MAX_VALUE in REQUIRED_TYPE_FIXTURES

    def test_int64(self):
        assert TypeFixture.INT64 in REQUIRED_TYPE_FIXTURES

    def test_null_missing(self):
        assert TypeFixture.NULL in REQUIRED_TYPE_FIXTURES
        assert TypeFixture.MISSING in REQUIRED_TYPE_FIXTURES

    def test_utf8(self):
        assert TypeFixture.UTF8 in REQUIRED_TYPE_FIXTURES

    def test_enum_unknown(self):
        assert TypeFixture.ENUM_UNKNOWN in REQUIRED_TYPE_FIXTURES

    def test_timestamp_duration(self):
        assert TypeFixture.TIMESTAMP in REQUIRED_TYPE_FIXTURES
        assert TypeFixture.DURATION in REQUIRED_TYPE_FIXTURES

    def test_buffer_bounds(self):
        assert TypeFixture.BUFFER_BOUNDS in REQUIRED_TYPE_FIXTURES


# ---------------------------------------------------------------------------
# Test Tiers
# ---------------------------------------------------------------------------

class TestTiers:
    def test_three_tiers(self):
        assert len(TestTier) == 3
        assert TestTier.TEST_FAST.value == "TEST_FAST"
        assert TestTier.TEST_STANDARD.value == "TEST_STANDARD"
        assert TestTier.TEST_FULL.value == "TEST_FULL"

    def test_fast_content(self):
        content = TIER_CONTENT[TestTier.TEST_FAST]
        assert TestCategory.UNIT in content
        assert TestCategory.BOUNDARY_CONTRACT in content

    def test_standard_content(self):
        content = TIER_CONTENT[TestTier.TEST_STANDARD]
        assert TestCategory.UNIT in content
        assert TestCategory.INTEGRATION in content
        assert TestCategory.FALLBACK in content

    def test_full_content(self):
        content = TIER_CONTENT[TestTier.TEST_FULL]
        assert content == REQUIRED_TEST_CATEGORIES

    def test_select_tier_dev(self):
        assert select_tier_by_env("dev") == TestTier.TEST_FAST
        assert select_tier_by_env("save") == TestTier.TEST_FAST

    def test_select_tier_ci(self):
        assert select_tier_by_env("ci") == TestTier.TEST_STANDARD
        assert select_tier_by_env("integration") == TestTier.TEST_STANDARD

    def test_select_tier_release(self):
        assert select_tier_by_env("release") == TestTier.TEST_FULL

    def test_release_must_run_full(self):
        """Release must execute TEST_FULL."""
        tier = select_tier_by_env("release")
        assert tier == TestTier.TEST_FULL
        assert TIER_CONTENT[tier] == REQUIRED_TEST_CATEGORIES


# ---------------------------------------------------------------------------
# Flaky Test Policy
# ---------------------------------------------------------------------------

class TestFlakyPolicy:
    def test_three_policies(self):
        assert len(FlakyPolicy) == 3

    def test_retry_not_allowed(self):
        assert FlakyPolicy.RETRY_NOT_ALLOWED.value == "retry_not_allowed"

    def test_quarantine(self):
        assert FlakyPolicy.QUARANTINE.value == "quarantine"

    def test_block_release(self):
        assert FlakyPolicy.BLOCK_RELEASE.value == "block_release"

    def test_critical_categories(self):
        assert TestCategory.BOUNDARY_CONTRACT in CRITICAL_TEST_CATEGORIES
        assert TestCategory.FAILURE in CRITICAL_TEST_CATEGORIES

    def test_classify_flaky_critical(self):
        """Critical ABI/contract test → block release."""
        policy = classify_flaky(TestCategory.BOUNDARY_CONTRACT)
        assert policy == FlakyPolicy.BLOCK_RELEASE

    def test_classify_flaky_failure(self):
        policy = classify_flaky(TestCategory.FAILURE)
        assert policy == FlakyPolicy.BLOCK_RELEASE

    def test_classify_flaky_non_critical(self):
        """Non-critical flaky → quarantine."""
        policy = classify_flaky(TestCategory.UNIT)
        assert policy == FlakyPolicy.QUARANTINE

    def test_classify_flaky_integration(self):
        policy = classify_flaky(TestCategory.INTEGRATION)
        assert policy == FlakyPolicy.QUARANTINE

    def test_flaky_record(self):
        record = FlakyTestRecord(
            test_id="test_boundary",
            category=TestCategory.BOUNDARY_CONTRACT,
            policy=FlakyPolicy.BLOCK_RELEASE,
            last_flaky_at="2024-01-01",
            flaky_count=3,
            blocks_release=True,
        )
        assert record.blocks_release
        assert record.flaky_count == 3


# ---------------------------------------------------------------------------
# Capability Test Coverage Record
# ---------------------------------------------------------------------------

class TestCapabilityCoverage:
    def _make_entry(self, **kwargs):
        defaults = dict(
            category=TestCategory.UNIT,
            test_id="test_unit",
            test_file="tests/test_unit.py",
            framework=TestFramework.PYTEST,
            language="python",
        )
        defaults.update(kwargs)
        return TestEntry(**defaults)

    def test_add_entry(self):
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(self._make_entry())
        assert len(cov.entries) == 1

    def test_has_category(self):
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(self._make_entry(category=TestCategory.UNIT))
        assert cov.has_category(TestCategory.UNIT)
        assert not cov.has_category(TestCategory.FAILURE)

    def test_missing_categories(self):
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(self._make_entry(category=TestCategory.UNIT))
        missing = cov.missing_categories()
        assert TestCategory.FAILURE in missing
        assert TestCategory.UNIT not in missing

    def test_is_complete(self):
        cov = CapabilityTestCoverage("cap1", "python")
        for cat in REQUIRED_TEST_CATEGORIES:
            cov.add_entry(self._make_entry(category=cat, test_id=f"test_{cat.value}"))
        assert cov.is_complete()

    def test_is_not_complete(self):
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(self._make_entry(category=TestCategory.UNIT))
        assert not cov.is_complete()

    def test_coverage_summary(self):
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(self._make_entry(category=TestCategory.UNIT))
        summary = cov.coverage_summary()
        assert summary["capability_id"] == "cap1"
        assert summary["complete"] is False
        assert "failure" in summary["missing"]

    def test_has_boundary_test(self):
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(self._make_entry(
            category=TestCategory.BOUNDARY_CONTRACT,
            boundary=BoundaryType.PYTHON_PYBIND11,
        ))
        assert cov.has_boundary_test(BoundaryType.PYTHON_PYBIND11)
        assert not cov.has_boundary_test(BoundaryType.C_ABI_CPP)

    def test_entries_by_category(self):
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(self._make_entry(category=TestCategory.UNIT, test_id="t1"))
        cov.add_entry(self._make_entry(category=TestCategory.FAILURE, test_id="t2"))
        unit_entries = cov.entries_by_category(TestCategory.UNIT)
        assert len(unit_entries) == 1
        assert unit_entries[0].test_id == "t1"


# ---------------------------------------------------------------------------
# Test Matrix Registry
# ---------------------------------------------------------------------------

class TestMatrixRegistryClass:
    def _make_complete_coverage(self, cap_id="cap1"):
        cov = CapabilityTestCoverage(cap_id, "python")
        for cat in REQUIRED_TEST_CATEGORIES:
            cov.add_entry(TestEntry(
                category=cat,
                test_id=f"test_{cap_id}_{cat.value}",
                test_file=f"tests/test_{cap_id}.py",
                framework=TestFramework.PYTEST,
                language="python",
            ))
        return cov

    def test_register_and_get(self):
        registry = TestMatrixRegistry()
        cov = self._make_complete_coverage()
        registry.register(cov)
        assert registry.get("cap1") is cov

    def test_all_capabilities(self):
        registry = TestMatrixRegistry()
        registry.register(self._make_complete_coverage("cap1"))
        registry.register(self._make_complete_coverage("cap2"))
        assert set(registry.all_capabilities()) == {"cap1", "cap2"}

    def test_incomplete_capabilities(self):
        registry = TestMatrixRegistry()
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(TestEntry(
            category=TestCategory.UNIT, test_id="t1",
            test_file="f.py", framework=TestFramework.PYTEST, language="python",
        ))
        registry.register(cov)
        assert "cap1" in registry.incomplete_capabilities()

    def test_no_incomplete_when_complete(self):
        registry = TestMatrixRegistry()
        registry.register(self._make_complete_coverage())
        assert registry.incomplete_capabilities() == []

    def test_coverage_report(self):
        registry = TestMatrixRegistry()
        registry.register(self._make_complete_coverage("cap1"))
        report = registry.coverage_report()
        assert report["total_capabilities"] == 1
        assert report["complete"] == 1
        assert report["incomplete"] == 0


# ---------------------------------------------------------------------------
# Native/Fallback Parity
# ---------------------------------------------------------------------------

class TestNativeFallbackParity:
    def test_parity_test_creation(self):
        pt = NativeFallbackParityTest(
            capability_id="cap1",
            corpus_id="corpus1",
            native_test_id="test_native",
            fallback_test_id="test_fallback",
        )
        assert pt.uses_same_corpus is True

    def test_verify_parity_same(self):
        assert verify_parity(42, 42) is True

    def test_verify_parity_different(self):
        assert verify_parity(42, 43) is False

    def test_verify_parity_same_structure(self):
        assert verify_parity({"a": 1}, {"a": 1}) is True

    def test_verify_parity_different_structure(self):
        assert verify_parity({"a": 1}, {"a": 2}) is False


# ---------------------------------------------------------------------------
# Fault Injection Coverage
# ---------------------------------------------------------------------------

class TestFaultInjection:
    def test_nine_types(self):
        assert len(FaultInjectionType) == 9

    def test_required_types(self):
        expected = {
            "abi_baseline", "sql_migration", "lifecycle_rollback",
            "timeout", "cancelled", "resource_exhaustion",
            "native_unavailable", "abi_mismatch", "csharp_platform_error",
        }
        actual = {ft.value for ft in FaultInjectionType}
        assert actual == expected

    def test_register_and_has_coverage(self):
        registry = FaultInjectionRegistry()
        registry.register(FaultInjectionCoverage(
            fault_type=FaultInjectionType.TIMEOUT,
            test_id="test_timeout",
            test_file="tests/test_timeout.py",
            framework=TestFramework.PYTEST,
        ))
        assert registry.has_coverage(FaultInjectionType.TIMEOUT)

    def test_missing_types(self):
        registry = FaultInjectionRegistry()
        missing = registry.missing_types()
        assert FaultInjectionType.TIMEOUT in missing

    def test_is_complete(self):
        registry = FaultInjectionRegistry()
        for ft in REQUIRED_FAULT_INJECTION:
            registry.register(FaultInjectionCoverage(
                fault_type=ft,
                test_id=f"test_{ft.value}",
                test_file=f"tests/test_{ft.value}.py",
                framework=TestFramework.PYTEST,
            ))
        assert registry.is_complete()

    def test_is_not_complete(self):
        registry = FaultInjectionRegistry()
        assert not registry.is_complete()

    def test_coverage_report(self):
        registry = FaultInjectionRegistry()
        report = registry.coverage_report()
        assert report["total_types"] == 9
        assert report["covered"] == 0
        assert report["complete"] is False


# ---------------------------------------------------------------------------
# Test Path Validation
# ---------------------------------------------------------------------------

class TestPathValidation:
    def test_valid_public_path(self):
        entry = TestEntry(
            category=TestCategory.UNIT, test_id="t1",
            test_file="f.py", framework=TestFramework.PYTEST,
            language="python", uses_public_path=True,
        )
        assert validate_test_path(entry)

    def test_invalid_private_path(self):
        entry = TestEntry(
            category=TestCategory.UNIT, test_id="t1",
            test_file="f.py", framework=TestFramework.PYTEST,
            language="python", uses_public_path=False,
        )
        assert not validate_test_path(entry)

    def test_integration_owned_by_python(self):
        """Integration tests are owned by Python pytest only."""
        entry = TestEntry(
            category=TestCategory.INTEGRATION, test_id="t1",
            test_file="f.py", framework=TestFramework.PYTEST,
            language="python",
        )
        assert validate_integration_owned_by_python(entry)

    def test_integration_not_owned_by_python(self):
        """Non-Python integration tests are not allowed."""
        entry = TestEntry(
            category=TestCategory.INTEGRATION, test_id="t1",
            test_file="f.ts", framework=TestFramework.VITEST,
            language="typescript",
        )
        assert not validate_integration_owned_by_python(entry)

    def test_non_integration_any_language(self):
        """Unit tests can be any language."""
        entry = TestEntry(
            category=TestCategory.UNIT, test_id="t1",
            test_file="f.ts", framework=TestFramework.VITEST,
            language="typescript",
        )
        assert validate_integration_owned_by_python(entry)


# ---------------------------------------------------------------------------
# Test Matrix Runner
# ---------------------------------------------------------------------------

class TestMatrixRunner:
    def _make_complete_registry(self):
        registry = TestMatrixRegistry()
        cov = CapabilityTestCoverage("cap1", "python")
        for cat in REQUIRED_TEST_CATEGORIES:
            cov.add_entry(TestEntry(
                category=cat, test_id=f"test_{cat.value}",
                test_file="f.py", framework=TestFramework.PYTEST,
                language="python",
            ))
        registry.register(cov)
        return registry

    def _make_complete_fault_registry(self):
        registry = FaultInjectionRegistry()
        for ft in REQUIRED_FAULT_INJECTION:
            registry.register(FaultInjectionCoverage(
                fault_type=ft, test_id=f"test_{ft.value}",
                test_file="f.py", framework=TestFramework.PYTEST,
            ))
        return registry

    def test_run_pass(self):
        registry = self._make_complete_registry()
        fault_registry = self._make_complete_fault_registry()
        result = run_test_matrix(
            registry, fault_registry, TestTier.TEST_FULL,
            passed=100, failed=0,
        )
        assert result.verdict == "PASS"
        assert result.total_tests == 100

    def test_run_fail(self):
        registry = self._make_complete_registry()
        fault_registry = self._make_complete_fault_registry()
        result = run_test_matrix(
            registry, fault_registry, TestTier.TEST_FULL,
            passed=90, failed=10,
        )
        assert result.verdict == "FAIL"
        assert result.failed == 10

    def test_run_incomplete_coverage(self):
        """TEST_FULL with incomplete coverage → INCOMPLETE."""
        registry = TestMatrixRegistry()
        cov = CapabilityTestCoverage("cap1", "python")
        cov.add_entry(TestEntry(
            category=TestCategory.UNIT, test_id="t1",
            test_file="f.py", framework=TestFramework.PYTEST, language="python",
        ))
        registry.register(cov)
        fault_registry = self._make_complete_fault_registry()
        result = run_test_matrix(
            registry, fault_registry, TestTier.TEST_FULL,
            passed=10, failed=0,
        )
        assert result.verdict == "INCOMPLETE"

    def test_run_blocking_flaky(self):
        """Critical flaky test blocks release."""
        registry = self._make_complete_registry()
        fault_registry = self._make_complete_fault_registry()
        flaky = [FlakyTestRecord(
            test_id="test_boundary",
            category=TestCategory.BOUNDARY_CONTRACT,
            policy=FlakyPolicy.BLOCK_RELEASE,
            last_flaky_at="2024-01-01",
            flaky_count=3,
            blocks_release=True,
        )]
        result = run_test_matrix(
            registry, fault_registry, TestTier.TEST_FULL,
            passed=100, failed=0, flaky_records=flaky,
        )
        assert result.verdict == "FAIL"

    def test_run_non_blocking_flaky(self):
        """Non-critical flaky test doesn't block."""
        registry = self._make_complete_registry()
        fault_registry = self._make_complete_fault_registry()
        flaky = [FlakyTestRecord(
            test_id="test_unit",
            category=TestCategory.UNIT,
            policy=FlakyPolicy.QUARANTINE,
            last_flaky_at="2024-01-01",
            flaky_count=2,
            quarantined=True,
            blocks_release=False,
        )]
        result = run_test_matrix(
            registry, fault_registry, TestTier.TEST_FULL,
            passed=100, failed=0, flaky_records=flaky,
        )
        assert result.verdict == "PASS"

    def test_result_to_dict(self):
        registry = self._make_complete_registry()
        fault_registry = self._make_complete_fault_registry()
        result = run_test_matrix(
            registry, fault_registry, TestTier.TEST_FAST,
            passed=10, failed=0,
        )
        d = result.to_dict()
        assert d["tier"] == "TEST_FAST"
        assert d["verdict"] == "PASS"
        assert d["passed"] == 10

    def test_release_requires_full(self):
        """Release must run TEST_FULL."""
        registry = self._make_complete_registry()
        fault_registry = self._make_complete_fault_registry()
        # TEST_FAST for release would be wrong, but the function allows it
        # The caller must select the right tier
        tier = select_tier_by_env("release")
        assert tier == TestTier.TEST_FULL
