"""Cross-Language Test Matrix Closure V1.

Consolidates all test coverage into a single canonical model based on
A210 (six-language-test-boundary):

    Test frameworks (fixed by A210):
        Python      — pytest
        TypeScript  — Vitest
        C++         — GoogleTest
        C           — native unit test / CTest runner
        C#          — xUnit
        SQL         — database transaction/schema tests

    Cross-module/cross-language integration is owned by Python pytest only.

    Capability Test Coverage Record:
        For each formal capability, records:
            owner unit test
            executor test
            boundary contract test
            fallback test
            failure test
            integration test

    Boundary test matrix:
        TypeScript↔Python contract
        Python↔SQL
        Python↔pybind11
        pybind11/C ABI
        C ABI↔C++
        C ABI/C# interop

    Canonical type fixtures:
        min/max, 64-bit integer, null/missing, UTF-8, enum unknown,
        timestamp/duration, buffer bounds

    Test tiers:
        TEST_FAST     — fast unit/contract tests (dev)
        TEST_STANDARD — unit + contract + integration (CI)
        TEST_FULL     — all tests + fault injection + native/fallback parity (release)

    Flaky test policy:
        Flaky tests cannot use auto-retry to fake PASS.
        Non-critical flaky → quarantine.
        Critical ABI/contract test → cannot release in failing state.

Rules:
    - No new test framework, governance Gate, language, or business owner.
    - All tests use formal public/boundary path — no private implementation bypass.
    - Each native capability runs same semantic corpus with native enabled + Python fallback.
    - Non-Python duplicate integration suites are removed/downgraded.
    - Each language keeps its own unit/contract tests.

Codex basis:
    A210 — six-language-test-boundary
    A208 — release verification
    A219 — Python fallback always preserved
    A220 — C ABI boundary
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


TEST_MATRIX_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Test frameworks (fixed by A210)
# ---------------------------------------------------------------------------

class TestFramework(str, Enum):
    """Canonical test framework per language (A210)."""
    PYTEST = "pytest"               # Python
    VITEST = "vitest"               # TypeScript
    GOTEST = "googletest"           # C++
    CTEST = "native-or-ctest"       # C
    XUNIT = "xunit"                # C#
    SQL_TEST = "database-transactional"  # SQL


# Language → framework mapping
LANGUAGE_FRAMEWORK: dict[str, TestFramework] = {
    "python": TestFramework.PYTEST,
    "typescript": TestFramework.VITEST,
    "cpp": TestFramework.GOTEST,
    "c": TestFramework.CTEST,
    "csharp": TestFramework.XUNIT,
    "sql": TestFramework.SQL_TEST,
}


# ---------------------------------------------------------------------------
# Test categories (per capability)
# ---------------------------------------------------------------------------

class TestCategory(str, Enum):
    """Test categories for Capability Test Coverage Record."""
    UNIT = "unit"                    # owner unit test (per-language)
    EXECUTOR = "executor"            # executor test
    BOUNDARY_CONTRACT = "boundary_contract"  # boundary contract test
    FALLBACK = "fallback"            # fallback test (A219)
    FAILURE = "failure"              # failure test (fault injection)
    INTEGRATION = "integration"       # cross-module/cross-language (Python pytest only)


# All required categories per capability
REQUIRED_TEST_CATEGORIES: frozenset[TestCategory] = frozenset(TestCategory)


# ---------------------------------------------------------------------------
# Boundary types
# ---------------------------------------------------------------------------

class BoundaryType(str, Enum):
    """Language boundary types that need contract tests."""
    TYPESCRIPT_PYTHON = "typescript_python"
    PYTHON_SQL = "python_sql"
    PYTHON_PYBIND11 = "python_pybind11"
    PYBIND11_C_ABI = "pybind11_c_abi"
    C_ABI_CPP = "c_abi_cpp"
    C_ABI_CSHARP = "c_abi_csharp"


# All boundaries
ALL_BOUNDARIES: frozenset[BoundaryType] = frozenset(BoundaryType)


# ---------------------------------------------------------------------------
# Canonical type fixtures
# ---------------------------------------------------------------------------

class TypeFixture(str, Enum):
    """Canonical type fixtures that must be covered."""
    MIN_VALUE = "min_value"
    MAX_VALUE = "max_value"
    INT64 = "int64"               # 64-bit integer
    NULL = "null"                 # null/missing
    MISSING = "missing"           # missing field
    UTF8 = "utf8"                 # UTF-8 multi-byte
    ENUM_UNKNOWN = "enum_unknown"  # unknown enum value
    TIMESTAMP = "timestamp"       # timestamp/duration
    DURATION = "duration"
    BUFFER_BOUNDS = "buffer_bounds"  # buffer boundary (0, max-1, max)


# All required type fixtures
REQUIRED_TYPE_FIXTURES: frozenset[TypeFixture] = frozenset(TypeFixture)


# ---------------------------------------------------------------------------
# Test tiers
# ---------------------------------------------------------------------------

class TestTier(str, Enum):
    """Test execution tiers."""
    TEST_FAST = "TEST_FAST"         # fast unit/contract (dev)
    TEST_STANDARD = "TEST_STANDARD"  # unit + contract + integration (CI)
    TEST_FULL = "TEST_FULL"          # all + fault injection + parity (release)


# Tier → what to run
TIER_CONTENT: dict[TestTier, frozenset[TestCategory]] = {
    TestTier.TEST_FAST: frozenset({
        TestCategory.UNIT,
        TestCategory.BOUNDARY_CONTRACT,
    }),
    TestTier.TEST_STANDARD: frozenset({
        TestCategory.UNIT,
        TestCategory.EXECUTOR,
        TestCategory.BOUNDARY_CONTRACT,
        TestCategory.FALLBACK,
        TestCategory.INTEGRATION,
    }),
    TestTier.TEST_FULL: frozenset(REQUIRED_TEST_CATEGORIES),
}


# Tier → environments
TIER_ENVIRONMENTS: dict[TestTier, tuple[str, ...]] = {
    TestTier.TEST_FAST: ("dev", "save"),
    TestTier.TEST_STANDARD: ("ci", "integration", "precommit"),
    TestTier.TEST_FULL: ("release",),
}


def select_tier_by_env(env: str) -> TestTier:
    """Select test tier by environment."""
    env_lower = env.lower()
    for tier, envs in TIER_ENVIRONMENTS.items():
        if env_lower in envs:
            return tier
    # Default: release → FULL
    return TestTier.TEST_FULL


# ---------------------------------------------------------------------------
# Flaky test policy
# ---------------------------------------------------------------------------

class FlakyPolicy(str, Enum):
    """Flaky test handling policy."""
    RETRY_NOT_ALLOWED = "retry_not_allowed"  # auto-retry cannot fake PASS
    QUARANTINE = "quarantine"                 # non-critical flaky → quarantine
    BLOCK_RELEASE = "block_release"           # critical ABI/contract → no release


# Categories that are critical — cannot release in failing state
CRITICAL_TEST_CATEGORIES: frozenset[TestCategory] = frozenset({
    TestCategory.BOUNDARY_CONTRACT,
    TestCategory.FAILURE,
})


@dataclass(frozen=True)
class FlakyTestRecord:
    """Record of a flaky test."""
    test_id: str
    category: TestCategory
    policy: FlakyPolicy
    last_flaky_at: str
    flaky_count: int
    quarantined: bool = False
    blocks_release: bool = False


def classify_flaky(
    category: TestCategory,
    *,
    flaky_count: int = 1,
) -> FlakyPolicy:
    """Classify a flaky test by its category.

    Flaky tests cannot use auto-retry to fake PASS.
    Non-critical flaky → quarantine.
    Critical ABI/contract test → block release.
    """
    if category in CRITICAL_TEST_CATEGORIES:
        return FlakyPolicy.BLOCK_RELEASE
    return FlakyPolicy.QUARANTINE


# ---------------------------------------------------------------------------
# Capability Test Coverage Record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestEntry:
    """One test entry in the coverage record."""
    category: TestCategory
    test_id: str             # e.g., "test_parser_token_estimate"
    test_file: str           # path to test file
    framework: TestFramework
    language: str            # test language
    boundary: BoundaryType | None = None  # for boundary tests
    uses_public_path: bool = True  # must use public/boundary path


@dataclass
class CapabilityTestCoverage:
    """Capability Test Coverage Record.

    For each formal capability, records all required test categories.
    """
    capability_id: str
    owner_language: str     # canonical owner language
    entries: list[TestEntry] = field(default_factory=list)

    def add_entry(self, entry: TestEntry) -> None:
        """Add a test entry."""
        self.entries.append(entry)

    def has_category(self, category: TestCategory) -> bool:
        """Check if a test category is covered."""
        return any(e.category == category for e in self.entries)

    def missing_categories(self) -> list[TestCategory]:
        """Get missing test categories."""
        return [
            cat for cat in REQUIRED_TEST_CATEGORIES
            if not self.has_category(cat)
        ]

    def is_complete(self) -> bool:
        """Check if all required categories are covered."""
        return not self.missing_categories()

    def coverage_summary(self) -> dict[str, Any]:
        """Get coverage summary."""
        covered = {cat.value: self.has_category(cat) for cat in REQUIRED_TEST_CATEGORIES}
        missing = [cat.value for cat in self.missing_categories()]
        return {
            "capability_id": self.capability_id,
            "owner_language": self.owner_language,
            "covered": covered,
            "missing": missing,
            "complete": self.is_complete(),
            "entry_count": len(self.entries),
        }

    def entries_by_category(self, category: TestCategory) -> list[TestEntry]:
        """Get entries for a specific category."""
        return [e for e in self.entries if e.category == category]

    def has_boundary_test(self, boundary: BoundaryType) -> bool:
        """Check if a specific boundary is tested."""
        return any(
            e.category == TestCategory.BOUNDARY_CONTRACT and e.boundary == boundary
            for e in self.entries
        )


# ---------------------------------------------------------------------------
# Test matrix registry
# ---------------------------------------------------------------------------

class TestMatrixRegistry:
    """Registry of all capability test coverage records.

    This is the canonical test matrix for the entire project.
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityTestCoverage] = {}

    def register(self, coverage: CapabilityTestCoverage) -> None:
        """Register a capability test coverage record."""
        self._capabilities[coverage.capability_id] = coverage

    def get(self, capability_id: str) -> CapabilityTestCoverage | None:
        """Get a capability's test coverage."""
        return self._capabilities.get(capability_id)

    def all_capabilities(self) -> list[str]:
        """Get all registered capability IDs."""
        return list(self._capabilities.keys())

    def incomplete_capabilities(self) -> list[str]:
        """Get capabilities with incomplete test coverage."""
        return [
            cap_id for cap_id, cov in self._capabilities.items()
            if not cov.is_complete()
        ]

    def missing_boundary_tests(self) -> dict[str, list[BoundaryType]]:
        """Get capabilities missing boundary tests."""
        result: dict[str, list[BoundaryType]] = {}
        for cap_id, cov in self._capabilities.items():
            missing = [
                b for b in ALL_BOUNDARIES
                if not cov.has_boundary_test(b)
            ]
            if missing:
                result[cap_id] = missing
        return result

    def coverage_report(self) -> dict[str, Any]:
        """Generate a full coverage report."""
        total = len(self._capabilities)
        complete = sum(1 for c in self._capabilities.values() if c.is_complete())
        incomplete = total - complete
        return {
            "total_capabilities": total,
            "complete": complete,
            "incomplete": incomplete,
            "incomplete_ids": self.incomplete_capabilities(),
            "capabilities": {
                cap_id: cov.coverage_summary()
                for cap_id, cov in self._capabilities.items()
            },
        }


# ---------------------------------------------------------------------------
# Native/fallback parity test
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NativeFallbackParityTest:
    """Native enabled vs Python fallback parity test.

    Each native capability must run the same semantic corpus with
    both native enabled and Python fallback.
    """
    capability_id: str
    corpus_id: str             # semantic corpus identifier
    native_test_id: str         # test with native enabled
    fallback_test_id: str       # test with Python fallback
    uses_same_corpus: bool = True  # must use same semantic corpus


def verify_parity(
    native_result: Any,
    fallback_result: Any,
) -> bool:
    """Verify that native and fallback produce the same result.

    The same semantic corpus must produce the same result.
    """
    return native_result == fallback_result


# ---------------------------------------------------------------------------
# Fault injection coverage
# ---------------------------------------------------------------------------

class FaultInjectionType(str, Enum):
    """Types of fault injection that must be covered."""
    ABI_BASELINE = "abi_baseline"               # ABI baseline test
    SQL_MIGRATION = "sql_migration"              # SQL N→N+1 migration
    LIFECYCLE_ROLLBACK = "lifecycle_rollback"     # lifecycle rollback
    TIMEOUT = "timeout"                          # TIMEOUT semantics
    CANCELLED = "cancelled"                      # CANCELLED semantics
    RESOURCE_EXHAUSTION = "resource_exhaustion"   # resource exhaustion
    NATIVE_UNAVAILABLE = "native_unavailable"     # native unavailable (A219)
    ABI_MISMATCH = "abi_mismatch"                 # ABI mismatch
    CSHARP_PLATFORM_ERROR = "csharp_platform_error"  # C# platform error


# All required fault injection types
REQUIRED_FAULT_INJECTION: frozenset[FaultInjectionType] = frozenset(FaultInjectionType)


@dataclass(frozen=True)
class FaultInjectionCoverage:
    """Coverage record for fault injection tests."""
    fault_type: FaultInjectionType
    test_id: str
    test_file: str
    framework: TestFramework
    covered: bool = True


class FaultInjectionRegistry:
    """Registry of fault injection test coverage."""

    def __init__(self) -> None:
        self._coverage: dict[FaultInjectionType, list[FaultInjectionCoverage]] = {}

    def register(self, coverage: FaultInjectionCoverage) -> None:
        """Register a fault injection test."""
        self._coverage.setdefault(coverage.fault_type, []).append(coverage)

    def has_coverage(self, fault_type: FaultInjectionType) -> bool:
        """Check if a fault type is covered."""
        entries = self._coverage.get(fault_type, [])
        return any(e.covered for e in entries)

    def missing_types(self) -> list[FaultInjectionType]:
        """Get missing fault injection types."""
        return [
            ft for ft in REQUIRED_FAULT_INJECTION
            if not self.has_coverage(ft)
        ]

    def is_complete(self) -> bool:
        """Check if all required fault injection types are covered."""
        return not self.missing_types()

    def coverage_report(self) -> dict[str, Any]:
        """Generate fault injection coverage report."""
        return {
            "total_types": len(REQUIRED_FAULT_INJECTION),
            "covered": sum(1 for ft in REQUIRED_FAULT_INJECTION if self.has_coverage(ft)),
            "missing": [ft.value for ft in self.missing_types()],
            "complete": self.is_complete(),
        }


# ---------------------------------------------------------------------------
# Test path validation
# ---------------------------------------------------------------------------

def validate_test_path(entry: TestEntry) -> bool:
    """Validate that a test uses the formal public/boundary path.

    All tests must use formal public/boundary path — no private
    implementation bypass to substitute for integration evidence.
    """
    return entry.uses_public_path


def validate_integration_owned_by_python(entry: TestEntry) -> bool:
    """Validate that integration tests are owned by Python pytest.

    Cross-module/cross-language integration is owned by Python pytest only.
    Non-Python duplicate integration suites are removed/downgraded.
    """
    if entry.category != TestCategory.INTEGRATION:
        return True  # not an integration test
    return entry.language == "python" and entry.framework == TestFramework.PYTEST


# ---------------------------------------------------------------------------
# Test matrix runner
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestMatrixResult:
    """Result of running the test matrix."""
    tier: TestTier
    total_tests: int
    passed: int
    failed: int
    skipped: int
    quarantined: int
    duration_ms: int
    verdict: str  # "PASS" | "FAIL" | "INCOMPLETE"
    coverage_report: dict[str, Any]
    fault_injection_report: dict[str, Any]
    flaky_records: list[FlakyTestRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "total_tests": self.total_tests,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "quarantined": self.quarantined,
            "duration_ms": self.duration_ms,
            "verdict": self.verdict,
            "coverage_report": self.coverage_report,
            "fault_injection_report": self.fault_injection_report,
            "flaky_records": [
                {
                    "test_id": r.test_id,
                    "category": r.category.value,
                    "policy": r.policy.value,
                    "quarantined": r.quarantined,
                    "blocks_release": r.blocks_release,
                }
                for r in self.flaky_records
            ],
        }


def run_test_matrix(
    registry: TestMatrixRegistry,
    fault_registry: FaultInjectionRegistry,
    tier: TestTier,
    *,
    passed: int = 0,
    failed: int = 0,
    skipped: int = 0,
    quarantined: int = 0,
    duration_ms: int = 0,
    flaky_records: list[FlakyTestRecord] | None = None,
) -> TestMatrixResult:
    """Run the test matrix and produce a result.

    For RELEASE, TEST_FULL must be run.
    Flaky tests cannot use auto-retry to fake PASS.
    Critical ABI/contract tests cannot release in failing state.
    """
    coverage = registry.coverage_report()
    fault_report = fault_registry.coverage_report()

    # Check if any critical flaky tests block release
    has_blocking_flaky = any(
        r.blocks_release for r in (flaky_records or [])
    )

    # Determine verdict
    if failed > 0:
        verdict = "FAIL"
    elif has_blocking_flaky and tier == TestTier.TEST_FULL:
        verdict = "FAIL"
    elif coverage["incomplete"] > 0 and tier == TestTier.TEST_FULL:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"

    return TestMatrixResult(
        tier=tier,
        total_tests=passed + failed + skipped + quarantined,
        passed=passed,
        failed=failed,
        skipped=skipped,
        quarantined=quarantined,
        duration_ms=duration_ms,
        verdict=verdict,
        coverage_report=coverage,
        fault_injection_report=fault_report,
        flaky_records=flaky_records or [],
    )


__all__ = [
    "TEST_MATRIX_VERSION",
    "TestFramework",
    "LANGUAGE_FRAMEWORK",
    "TestCategory",
    "REQUIRED_TEST_CATEGORIES",
    "BoundaryType",
    "ALL_BOUNDARIES",
    "TypeFixture",
    "REQUIRED_TYPE_FIXTURES",
    "TestTier",
    "TIER_CONTENT",
    "TIER_ENVIRONMENTS",
    "select_tier_by_env",
    "FlakyPolicy",
    "CRITICAL_TEST_CATEGORIES",
    "FlakyTestRecord",
    "classify_flaky",
    "TestEntry",
    "CapabilityTestCoverage",
    "TestMatrixRegistry",
    "NativeFallbackParityTest",
    "verify_parity",
    "FaultInjectionType",
    "REQUIRED_FAULT_INJECTION",
    "FaultInjectionCoverage",
    "FaultInjectionRegistry",
    "validate_test_path",
    "validate_integration_owned_by_python",
    "TestMatrixResult",
    "run_test_matrix",
]
