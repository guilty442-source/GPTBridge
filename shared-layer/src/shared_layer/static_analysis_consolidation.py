"""Language Standards & Static Analysis Consolidation V1.

Consolidates all language standards and static analysis into a single
canonical model:

    Language baselines (project-level, fixed):
        C       — C17
        C++     — C++20
        Python  — strict typing on public/cross-language/database/native/C# boundary
        TypeScript — strict, strictNullChecks, noImplicitAny, noImplicitReturns,
                     noFallthroughCasesInSwitch (baseline);
                     noUncheckedIndexedAccess/exactOptionalPropertyTypes (phased)
        C#      — nullable analysis, C ABI/Win32 interop width/layout/convention
        SQL     — PostgreSQL/SQLite dialect: parameterization, NULL, unbounded query,
                  migration/schema dependency

    StaticFinding schema (unified):
        language, rule, severity, file, line, message, category, tool, evidence

    Severity mapping:
        CRITICAL — must fix before merge (contract/ABI/lifetime/exception boundary)
        HIGH     — should fix before merge (type safety, security)
        MEDIUM   — should fix soon (style, maintainability)
        LOW      — informational

    Tiered checks:
        SAVE       — affected-file fast checks only
        PRECOMMIT  — affected module
        INTEGRATION — full six-language static verification
        RELEASE    — full six-language static verification + legacy baseline diff

    Legacy baseline:
        Historical violations are recorded in a versioned baseline.
        Baseline is a debt that must decrease — never increase.
        Baseline is NOT a permanent exemption.

Rules:
    - No new language, governance Gate, business owner, or architecture layer.
    - Formatter may auto-fix pure formatting only.
    - Semantic findings (type/lifetime/ABI/contract) are never auto-fixed.
    - Third-party analyzer defaults do NOT become Codex policy.
    - Every blocking rule must have a current Codex/LanguagePolicy basis.
    - GateEvidence/Final Gate integration — no second governance verdict.

Codex basis:
    A205 — API boundary
    A204/A220 — C ABI boundary
    A211 — six-language canonical roles
    A348/A351/A352/A353 — language boundary gate
    A430/E160 — source-size compliance
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


STATIC_ANALYSIS_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Language standards (project-level, fixed)
# ---------------------------------------------------------------------------

class LanguageStandard(str, Enum):
    """Canonical language standard for each GPTBridge language."""
    C = "C17"
    CPP = "C++20"
    PYTHON = "strict-typing-boundary"
    TYPESCRIPT = "strict"
    CSHARP = "nullable-analysis"
    SQL = "dialect-aware"


# Language-specific analysis focus
LANGUAGE_ANALYSIS_FOCUS: dict[str, tuple[str, ...]] = {
    "c": (
        "integer_conversion_overflow",
        "buffer_bounds",
        "lifetime_safety",
        "exception_boundary",  # C has no exceptions, but C++ interop
        "thread_safety",
        "uninitialized_data",
    ),
    "cpp": (
        "integer_conversion_overflow",
        "buffer_bounds",
        "lifetime_safety",
        "exception_boundary",  # must not cross C ABI
        "thread_safety",
        "uninitialized_data",
    ),
    "python": (
        "strict_typing_boundary",  # public/cross-language/database/native/C#
        "optional_checking",
        "type_safety",
        "boundary_contract",
    ),
    "typescript": (
        "strict_mode",
        "strict_null_checks",
        "no_implicit_any",
        "no_implicit_returns",
        "no_fallthrough_cases",
        "no_unchecked_indexed_access",  # phased
        "exact_optional_property_types",  # phased
    ),
    "csharp": (
        "nullable_analysis",
        "c_abi_interop_width",
        "c_abi_interop_layout",
        "calling_convention",
        "handle_lifetime",
    ),
    "sql": (
        "parameterization",
        "null_handling",
        "unbounded_query",
        "migration_schema_dependency",
    ),
}


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """Finding severity levels."""
    CRITICAL = "CRITICAL"   # must fix before merge
    HIGH = "HIGH"          # should fix before merge
    MEDIUM = "MEDIUM"       # should fix soon
    LOW = "LOW"            # informational


# Severity ordering for comparison
_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def severity_at_least(a: Severity, b: Severity) -> bool:
    """Check if severity a is at least as severe as b."""
    return _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b]


# Rules that block merge (must have Codex/LanguagePolicy basis)
BLOCKING_SEVERITIES: frozenset[Severity] = frozenset({
    Severity.CRITICAL,
    Severity.HIGH,
})


# ---------------------------------------------------------------------------
# Finding categories
# ---------------------------------------------------------------------------

class FindingCategory(str, Enum):
    """Categories of static analysis findings."""
    # C/C++ categories
    INTEGER_OVERFLOW = "integer_overflow"
    BUFFER_BOUNDS = "buffer_bounds"
    LIFETIME_SAFETY = "lifetime_safety"
    EXCEPTION_BOUNDARY = "exception_boundary"
    THREAD_SAFETY = "thread_safety"
    UNINITIALIZED_DATA = "uninitialized_data"
    # Python categories
    TYPE_SAFETY = "type_safety"
    OPTIONAL_CHECKING = "optional_checking"
    BOUNDARY_CONTRACT = "boundary_contract"
    # TypeScript categories
    STRICT_MODE = "strict_mode"
    NULL_SAFETY = "null_safety"
    IMPLICIT_ANY = "implicit_any"
    # C# categories
    NULLABLE_ANALYSIS = "nullable_analysis"
    INTEROP_WIDTH = "interop_width"
    INTEROP_LAYOUT = "interop_layout"
    CALLING_CONVENTION = "calling_convention"
    HANDLE_LIFETIME = "handle_lifetime"
    # SQL categories
    PARAMETERIZATION = "parameterization"
    NULL_HANDLING = "null_handling"
    UNBOUNDED_QUERY = "unbounded_query"
    MIGRATION_DEPENDENCY = "migration_dependency"
    # General
    FORMATTING = "formatting"
    STYLE = "style"
    OTHER = "other"


# Category → default severity mapping
_CATEGORY_SEVERITY: dict[FindingCategory, Severity] = {
    # C/C++ — high/critical
    FindingCategory.INTEGER_OVERFLOW: Severity.CRITICAL,
    FindingCategory.BUFFER_BOUNDS: Severity.CRITICAL,
    FindingCategory.LIFETIME_SAFETY: Severity.CRITICAL,
    FindingCategory.EXCEPTION_BOUNDARY: Severity.CRITICAL,
    FindingCategory.THREAD_SAFETY: Severity.HIGH,
    FindingCategory.UNINITIALIZED_DATA: Severity.HIGH,
    # Python — high
    FindingCategory.TYPE_SAFETY: Severity.HIGH,
    FindingCategory.OPTIONAL_CHECKING: Severity.MEDIUM,
    FindingCategory.BOUNDARY_CONTRACT: Severity.CRITICAL,
    # TypeScript — medium/high
    FindingCategory.STRICT_MODE: Severity.HIGH,
    FindingCategory.NULL_SAFETY: Severity.HIGH,
    FindingCategory.IMPLICIT_ANY: Severity.MEDIUM,
    # C# — high/critical
    FindingCategory.NULLABLE_ANALYSIS: Severity.HIGH,
    FindingCategory.INTEROP_WIDTH: Severity.CRITICAL,
    FindingCategory.INTEROP_LAYOUT: Severity.CRITICAL,
    FindingCategory.CALLING_CONVENTION: Severity.CRITICAL,
    FindingCategory.HANDLE_LIFETIME: Severity.HIGH,
    # SQL — high
    FindingCategory.PARAMETERIZATION: Severity.CRITICAL,
    FindingCategory.NULL_HANDLING: Severity.HIGH,
    FindingCategory.UNBOUNDED_QUERY: Severity.HIGH,
    FindingCategory.MIGRATION_DEPENDENCY: Severity.HIGH,
    # General
    FindingCategory.FORMATTING: Severity.LOW,
    FindingCategory.STYLE: Severity.LOW,
    FindingCategory.OTHER: Severity.MEDIUM,
}


def default_severity(category: FindingCategory) -> Severity:
    """Get the default severity for a finding category."""
    return _CATEGORY_SEVERITY.get(category, Severity.MEDIUM)


# ---------------------------------------------------------------------------
# StaticFinding schema (unified)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StaticFinding:
    """Unified static analysis finding.

    Every finding from every language/tool is mapped to this schema.
    This is the only finding representation shared across languages.
    """
    language: str           # "c", "cpp", "python", "typescript", "csharp", "sql"
    rule: str               # rule identifier (e.g., "cpp:integer_overflow")
    severity: Severity
    category: FindingCategory
    file: str               # file path
    line: int               # line number (0 if not applicable)
    message: str            # human-readable message
    tool: str               # analyzer that produced this finding
    evidence: str = ""      # code snippet or evidence
    codex_basis: str = ""   # Codex/LanguagePolicy basis (required for blocking)
    is_blocking: bool = False
    is_auto_fixable: bool = False  # formatter can auto-fix (formatting only)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "rule": self.rule,
            "severity": self.severity.value,
            "category": self.category.value,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "tool": self.tool,
            "evidence": self.evidence,
            "codex_basis": self.codex_basis,
            "is_blocking": self.is_blocking,
            "is_auto_fixable": self.is_auto_fixable,
        }


# ---------------------------------------------------------------------------
# Legacy baseline (versioned, decreasing only)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaselineEntry:
    """One entry in the legacy baseline.

    Baseline is a debt that must decrease — never increase.
    Baseline is NOT a permanent exemption.
    """
    finding: StaticFinding
    added_at: str         # ISO timestamp
    added_by: str          # who added this entry
    reason: str            # why it was baselined
    expected_fix_version: str = ""  # when it should be fixed


class LegacyBaseline:
    """Versioned legacy baseline for historical violations.

    The baseline records findings that exist in the codebase but are
    not yet fixed.  It is a debt that must decrease — never increase.

    Rules:
        - New findings are NOT added to the baseline (no new baseline debt).
        - Fixed findings are removed from the baseline.
        - Baseline is NOT a permanent exemption.
        - Baseline count must decrease over time.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save([])

    def _load(self) -> list[dict[str, Any]]:
        data = self.path.read_text(encoding="utf-8")
        return json.loads(data) if data.strip() else []

    def _save(self, entries: list[dict[str, Any]]) -> None:
        self.path.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def add(self, entry: BaselineEntry) -> str:
        """Add a finding to the baseline.

        WARNING: adding to baseline creates debt.  This should only
        be done for pre-existing historical violations, never for
        new violations.
        """
        entries = self._load()
        key = _finding_key(entry.finding)
        # Don't add duplicates
        for e in entries:
            if _finding_key(StaticFinding(**e["finding"])) == key:
                return key
        entries.append({
            "finding": asdict(entry.finding),
            "added_at": entry.added_at,
            "added_by": entry.added_by,
            "reason": entry.reason,
            "expected_fix_version": entry.expected_fix_version,
        })
        self._save(entries)
        return key

    def remove(self, finding: StaticFinding) -> bool:
        """Remove a finding from the baseline (when it's fixed)."""
        entries = self._load()
        key = _finding_key(finding)
        original_len = len(entries)
        entries = [
            e for e in entries
            if _finding_key(StaticFinding(**e["finding"])) != key
        ]
        if len(entries) < original_len:
            self._save(entries)
            return True
        return False

    def contains(self, finding: StaticFinding) -> bool:
        """Check if a finding is in the baseline."""
        entries = self._load()
        key = _finding_key(finding)
        return any(
            _finding_key(StaticFinding(**e["finding"])) == key
            for e in entries
        )

    def count(self) -> int:
        """Number of findings in the baseline."""
        return len(self._load())

    def count_by_severity(self) -> dict[str, int]:
        """Count findings by severity."""
        entries = self._load()
        counts: dict[str, int] = {}
        for e in entries:
            sev = e["finding"]["severity"]
            counts[sev] = counts.get(sev, 0) + 1
        return counts

    def all_entries(self) -> list[BaselineEntry]:
        """Get all baseline entries."""
        entries = self._load()
        return [
            BaselineEntry(
                finding=StaticFinding(**e["finding"]),
                added_at=e["added_at"],
                added_by=e["added_by"],
                reason=e["reason"],
                expected_fix_version=e.get("expected_fix_version", ""),
            )
            for e in entries
        ]

    def is_debt_increasing(self, new_findings: list[StaticFinding]) -> bool:
        """Check if adding new findings would increase baseline debt.

        New findings that are NOT in the baseline would increase debt.
        This is forbidden — no new baseline debt.
        """
        for finding in new_findings:
            if not self.contains(finding):
                return True
        return False


def _finding_key(finding: StaticFinding) -> str:
    """Generate a unique key for a finding."""
    return f"{finding.language}:{finding.rule}:{finding.file}:{finding.line}"


# ---------------------------------------------------------------------------
# Check tiers
# ---------------------------------------------------------------------------

class CheckTier(str, Enum):
    """Static analysis check tiers."""
    SAVE = "SAVE"               # affected-file fast checks only
    PRECOMMIT = "PRECOMMIT"     # affected module
    INTEGRATION = "INTEGRATION"  # full six-language static verification
    RELEASE = "RELEASE"          # full + legacy baseline diff


# Tier → what to check
TIER_SCOPE: dict[CheckTier, str] = {
    CheckTier.SAVE: "affected-file",
    CheckTier.PRECOMMIT: "affected-module",
    CheckTier.INTEGRATION: "full-six-language",
    CheckTier.RELEASE: "full-six-language-plus-baseline-diff",
}


# ---------------------------------------------------------------------------
# Gate evidence (integrates with existing GateEvidence/Final Gate)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StaticAnalysisGateEvidence:
    """Gate evidence from static analysis, for existing GateEvidence/Final Gate.

    This does NOT create a second governance verdict — it feeds findings
    into the existing gate infrastructure.
    """
    tier: CheckTier
    findings: tuple[StaticFinding, ...]
    blocking_count: int
    warning_count: int
    baseline_count: int
    new_debt_count: int  # findings not in baseline (should be 0)
    timestamp: float
    duration_ms: int
    verdict: str  # "PASS" | "WARN" | "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "findings": [f.to_dict() for f in self.findings],
            "blocking_count": self.blocking_count,
            "warning_count": self.warning_count,
            "baseline_count": self.baseline_count,
            "new_debt_count": self.new_debt_count,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "verdict": self.verdict,
        }

    def to_gate_evidence(self) -> dict[str, Any]:
        """Convert to GateEvidence format for existing gate infrastructure."""
        return {
            "gate_type": "static_analysis",
            "tier": self.tier.value,
            "verdict": self.verdict,
            "blocking_findings": [
                f.to_dict() for f in self.findings if f.is_blocking
            ],
            "warning_findings": [
                f.to_dict() for f in self.findings
                if not f.is_blocking and f.severity != Severity.LOW
            ],
            "summary": {
                "total": len(self.findings),
                "blocking": self.blocking_count,
                "warning": self.warning_count,
                "baseline": self.baseline_count,
                "new_debt": self.new_debt_count,
            },
        }


def compute_verdict(
    findings: list[StaticFinding],
    baseline: LegacyBaseline,
) -> tuple[str, int, int, int]:
    """Compute the gate verdict from findings.

    Returns (verdict, blocking_count, warning_count, new_debt_count).
    """
    blocking = 0
    warning = 0
    new_debt = 0

    for finding in findings:
        # Check if finding is in baseline (historical violation)
        in_baseline = baseline.contains(finding)

        if finding.is_blocking or finding.severity in BLOCKING_SEVERITIES:
            if not in_baseline:
                # New blocking finding not in baseline → FAIL
                blocking += 1
                new_debt += 1
            # else: historical violation in baseline → don't block
        elif finding.severity == Severity.MEDIUM:
            warning += 1
            if not in_baseline:
                new_debt += 1

    if blocking > 0:
        return "FAIL", blocking, warning, new_debt
    if warning > 0:
        return "WARN", blocking, warning, new_debt
    return "PASS", blocking, warning, new_debt


def run_static_analysis(
    findings: list[StaticFinding],
    baseline: LegacyBaseline,
    tier: CheckTier,
    *,
    duration_ms: int = 0,
) -> StaticAnalysisGateEvidence:
    """Run static analysis and produce gate evidence.

    This integrates with the existing GateEvidence/Final Gate — it does
    NOT create a second governance verdict.
    """
    verdict, blocking, warning, new_debt = compute_verdict(findings, baseline)

    return StaticAnalysisGateEvidence(
        tier=tier,
        findings=tuple(findings),
        blocking_count=blocking,
        warning_count=warning,
        baseline_count=baseline.count(),
        new_debt_count=new_debt,
        timestamp=time.time(),
        duration_ms=duration_ms,
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# Auto-fix policy
# ---------------------------------------------------------------------------

# Categories that can be auto-fixed by formatter
AUTO_FIXABLE_CATEGORIES: frozenset[FindingCategory] = frozenset({
    FindingCategory.FORMATTING,
})

# Categories that must NEVER be auto-fixed (semantic findings)
NEVER_AUTO_FIX_CATEGORIES: frozenset[FindingCategory] = frozenset({
    FindingCategory.INTEGER_OVERFLOW,
    FindingCategory.BUFFER_BOUNDS,
    FindingCategory.LIFETIME_SAFETY,
    FindingCategory.EXCEPTION_BOUNDARY,
    FindingCategory.THREAD_SAFETY,
    FindingCategory.UNINITIALIZED_DATA,
    FindingCategory.TYPE_SAFETY,
    FindingCategory.BOUNDARY_CONTRACT,
    FindingCategory.INTEROP_WIDTH,
    FindingCategory.INTEROP_LAYOUT,
    FindingCategory.CALLING_CONVENTION,
    FindingCategory.HANDLE_LIFETIME,
    FindingCategory.PARAMETERIZATION,
})


def is_auto_fixable(category: FindingCategory) -> bool:
    """Check if a finding category can be auto-fixed.

    Formatter may auto-fix pure formatting only.
    Semantic findings (type/lifetime/ABI/contract) are never auto-fixed.
    """
    if category in NEVER_AUTO_FIX_CATEGORIES:
        return False
    return category in AUTO_FIXABLE_CATEGORIES


# ---------------------------------------------------------------------------
# Codex basis validation
# ---------------------------------------------------------------------------

# Every blocking rule must have a current Codex/LanguagePolicy basis.
# Third-party analyzer defaults do NOT become Codex policy.

def validate_codex_basis(finding: StaticFinding) -> bool:
    """Validate that a blocking finding has a Codex/LanguagePolicy basis.

    Third-party analyzer defaults do NOT become Codex policy.
    Every blocking rule must have a current Codex/LanguagePolicy basis.
    """
    if not finding.is_blocking:
        return True  # non-blocking findings don't need Codex basis
    return bool(finding.codex_basis)


def filter_third_party_defaults(
    findings: list[StaticFinding],
) -> list[StaticFinding]:
    """Filter out third-party analyzer defaults without Codex basis.

    Third-party analyzer defaults do NOT become Codex policy.
    Only findings with a Codex/LanguagePolicy basis are kept as blocking.
    """
    result: list[StaticFinding] = []
    for f in findings:
        if f.is_blocking and not validate_codex_basis(f):
            # Downgrade to non-blocking if no Codex basis
            result.append(StaticFinding(
                language=f.language,
                rule=f.rule,
                severity=f.severity,
                category=f.category,
                file=f.file,
                line=f.line,
                message=f.message,
                tool=f.tool,
                evidence=f.evidence,
                codex_basis=f.codex_basis,
                is_blocking=False,  # downgraded
                is_auto_fixable=f.is_auto_fixable,
            ))
        else:
            result.append(f)
    return result


# ---------------------------------------------------------------------------
# Language baseline configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LanguageBaseline:
    """Baseline configuration for one language."""
    language: str
    standard: LanguageStandard
    analysis_focus: tuple[str, ...]
    compiler_flags: tuple[str, ...] = ()
    linter_config: dict[str, Any] = field(default_factory=dict)


# Project-level language baselines (fixed)
LANGUAGE_BASELINES: dict[str, LanguageBaseline] = {
    "c": LanguageBaseline(
        language="c",
        standard=LanguageStandard.C,
        analysis_focus=LANGUAGE_ANALYSIS_FOCUS["c"],
        compiler_flags=("/std:c17",),
    ),
    "cpp": LanguageBaseline(
        language="cpp",
        standard=LanguageStandard.CPP,
        analysis_focus=LANGUAGE_ANALYSIS_FOCUS["cpp"],
        compiler_flags=("/std:c++20",),
    ),
    "python": LanguageBaseline(
        language="python",
        standard=LanguageStandard.PYTHON,
        analysis_focus=LANGUAGE_ANALYSIS_FOCUS["python"],
        linter_config={
            "strict_typing_boundary": True,
            "optional_checking": True,
        },
    ),
    "typescript": LanguageBaseline(
        language="typescript",
        standard=LanguageStandard.TYPESCRIPT,
        analysis_focus=LANGUAGE_ANALYSIS_FOCUS["typescript"],
        linter_config={
            "strict": True,
            "strictNullChecks": True,
            "noImplicitAny": True,
            "noImplicitReturns": True,
            "noFallthroughCasesInSwitch": True,
            "noUncheckedIndexedAccess": "phased",
            "exactOptionalPropertyTypes": "phased",
        },
    ),
    "csharp": LanguageBaseline(
        language="csharp",
        standard=LanguageStandard.CSHARP,
        analysis_focus=LANGUAGE_ANALYSIS_FOCUS["csharp"],
        linter_config={
            "nullable_analysis": True,
            "interop_width_check": True,
            "interop_layout_check": True,
            "calling_convention_check": True,
        },
    ),
    "sql": LanguageBaseline(
        language="sql",
        standard=LanguageStandard.SQL,
        analysis_focus=LANGUAGE_ANALYSIS_FOCUS["sql"],
        linter_config={
            "parameterization_check": True,
            "null_handling_check": True,
            "unbounded_query_check": True,
            "migration_dependency_check": True,
        },
    ),
}


def get_language_baseline(language: str) -> LanguageBaseline | None:
    """Get the baseline configuration for a language."""
    return LANGUAGE_BASELINES.get(language.lower())


def all_language_baselines() -> dict[str, LanguageBaseline]:
    """Get all language baselines."""
    return dict(LANGUAGE_BASELINES)


__all__ = [
    "STATIC_ANALYSIS_VERSION",
    "LanguageStandard",
    "LANGUAGE_ANALYSIS_FOCUS",
    "Severity",
    "severity_at_least",
    "BLOCKING_SEVERITIES",
    "FindingCategory",
    "default_severity",
    "StaticFinding",
    "BaselineEntry",
    "LegacyBaseline",
    "CheckTier",
    "TIER_SCOPE",
    "StaticAnalysisGateEvidence",
    "compute_verdict",
    "run_static_analysis",
    "AUTO_FIXABLE_CATEGORIES",
    "NEVER_AUTO_FIX_CATEGORIES",
    "is_auto_fixable",
    "validate_codex_basis",
    "filter_third_party_defaults",
    "LanguageBaseline",
    "LANGUAGE_BASELINES",
    "get_language_baseline",
    "all_language_baselines",
]
