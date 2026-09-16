"""Tests for Language Standards & Static Analysis Consolidation V1.

Tests the full static analysis pipeline:
    - Language standards (C17, C++20, Python, TypeScript, C#, SQL)
    - Severity mapping (CRITICAL/HIGH/MEDIUM/LOW)
    - StaticFinding schema (unified)
    - Legacy baseline (versioned, decreasing only)
    - Check tiers (SAVE/PRECOMMIT/INTEGRATION/RELEASE)
    - Gate evidence (integrates with existing GateEvidence/Final Gate)
    - Auto-fix policy (formatting only, never semantic)
    - Codex basis validation (third-party defaults don't become policy)

Codex basis:
    A205 — API boundary
    A204/A220 — C ABI boundary
    A211 — six-language canonical roles
    A348/A351/A352/A353 — language boundary gate
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

import pytest

from shared_layer.static_analysis_consolidation import (
    STATIC_ANALYSIS_VERSION,
    LanguageStandard,
    LANGUAGE_ANALYSIS_FOCUS,
    Severity,
    severity_at_least,
    BLOCKING_SEVERITIES,
    FindingCategory,
    default_severity,
    StaticFinding,
    BaselineEntry,
    LegacyBaseline,
    CheckTier,
    TIER_SCOPE,
    StaticAnalysisGateEvidence,
    compute_verdict,
    run_static_analysis,
    AUTO_FIXABLE_CATEGORIES,
    NEVER_AUTO_FIX_CATEGORIES,
    is_auto_fixable,
    validate_codex_basis,
    filter_third_party_defaults,
    LanguageBaseline,
    LANGUAGE_BASELINES,
    get_language_baseline,
    all_language_baselines,
)


# ---------------------------------------------------------------------------
# Language Standards
# ---------------------------------------------------------------------------

class TestLanguageStandards:
    def test_c_is_c17(self):
        assert LanguageStandard.C.value == "C17"

    def test_cpp_is_c20(self):
        assert LanguageStandard.CPP.value == "C++20"

    def test_python_strict_typing(self):
        assert "strict" in LanguageStandard.PYTHON.value.lower()

    def test_typescript_strict(self):
        assert LanguageStandard.TYPESCRIPT.value == "strict"

    def test_csharp_nullable(self):
        assert "nullable" in LanguageStandard.CSHARP.value.lower()

    def test_sql_dialect_aware(self):
        assert "dialect" in LanguageStandard.SQL.value.lower()

    def test_six_languages(self):
        assert len(LanguageStandard) == 6

    def test_analysis_focus_c(self):
        focus = LANGUAGE_ANALYSIS_FOCUS["c"]
        assert "integer_conversion_overflow" in focus
        assert "buffer_bounds" in focus
        assert "lifetime_safety" in focus
        assert "thread_safety" in focus
        assert "uninitialized_data" in focus

    def test_analysis_focus_cpp(self):
        focus = LANGUAGE_ANALYSIS_FOCUS["cpp"]
        assert "exception_boundary" in focus

    def test_analysis_focus_python(self):
        focus = LANGUAGE_ANALYSIS_FOCUS["python"]
        assert "strict_typing_boundary" in focus
        assert "optional_checking" in focus

    def test_analysis_focus_typescript(self):
        focus = LANGUAGE_ANALYSIS_FOCUS["typescript"]
        assert "strict_mode" in focus
        assert "strict_null_checks" in focus
        assert "no_implicit_any" in focus
        assert "no_implicit_returns" in focus
        assert "no_fallthrough_cases" in focus

    def test_analysis_focus_csharp(self):
        focus = LANGUAGE_ANALYSIS_FOCUS["csharp"]
        assert "nullable_analysis" in focus
        assert "c_abi_interop_width" in focus
        assert "c_abi_interop_layout" in focus
        assert "calling_convention" in focus
        assert "handle_lifetime" in focus

    def test_analysis_focus_sql(self):
        focus = LANGUAGE_ANALYSIS_FOCUS["sql"]
        assert "parameterization" in focus
        assert "null_handling" in focus
        assert "unbounded_query" in focus
        assert "migration_schema_dependency" in focus


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class TestSeverity:
    def test_four_severities(self):
        assert len(Severity) == 4
        assert Severity.CRITICAL.value == "CRITICAL"
        assert Severity.HIGH.value == "HIGH"
        assert Severity.MEDIUM.value == "MEDIUM"
        assert Severity.LOW.value == "LOW"

    def test_blocking_severities(self):
        assert Severity.CRITICAL in BLOCKING_SEVERITIES
        assert Severity.HIGH in BLOCKING_SEVERITIES
        assert Severity.MEDIUM not in BLOCKING_SEVERITIES
        assert Severity.LOW not in BLOCKING_SEVERITIES

    def test_severity_at_least(self):
        assert severity_at_least(Severity.CRITICAL, Severity.HIGH)
        assert severity_at_least(Severity.CRITICAL, Severity.CRITICAL)
        assert not severity_at_least(Severity.LOW, Severity.HIGH)

    def test_default_severity_integer_overflow(self):
        assert default_severity(FindingCategory.INTEGER_OVERFLOW) == Severity.CRITICAL

    def test_default_severity_buffer_bounds(self):
        assert default_severity(FindingCategory.BUFFER_BOUNDS) == Severity.CRITICAL

    def test_default_severity_lifetime(self):
        assert default_severity(FindingCategory.LIFETIME_SAFETY) == Severity.CRITICAL

    def test_default_severity_exception_boundary(self):
        assert default_severity(FindingCategory.EXCEPTION_BOUNDARY) == Severity.CRITICAL

    def test_default_severity_parameterization(self):
        assert default_severity(FindingCategory.PARAMETERIZATION) == Severity.CRITICAL

    def test_default_severity_formatting(self):
        assert default_severity(FindingCategory.FORMATTING) == Severity.LOW


# ---------------------------------------------------------------------------
# StaticFinding
# ---------------------------------------------------------------------------

class TestStaticFinding:
    def _make_finding(self, **kwargs):
        defaults = dict(
            language="cpp",
            rule="cpp:integer_overflow",
            severity=Severity.CRITICAL,
            category=FindingCategory.INTEGER_OVERFLOW,
            file="native/core/parser.cpp",
            line=42,
            message="integer overflow in conversion",
            tool="clang-tidy",
        )
        defaults.update(kwargs)
        return StaticFinding(**defaults)

    def test_create_finding(self):
        f = self._make_finding()
        assert f.language == "cpp"
        assert f.severity == Severity.CRITICAL
        assert f.category == FindingCategory.INTEGER_OVERFLOW

    def test_to_dict(self):
        f = self._make_finding()
        d = f.to_dict()
        assert d["language"] == "cpp"
        assert d["severity"] == "CRITICAL"
        assert d["category"] == "integer_overflow"

    def test_codex_basis(self):
        f = self._make_finding(codex_basis="A204", is_blocking=True)
        assert f.codex_basis == "A204"
        assert f.is_blocking

    def test_auto_fixable_flag(self):
        f = self._make_finding(is_auto_fixable=True)
        assert f.is_auto_fixable


# ---------------------------------------------------------------------------
# Legacy Baseline
# ---------------------------------------------------------------------------

class TestLegacyBaseline:
    def _make_finding(self, **kwargs):
        defaults = dict(
            language="cpp",
            rule="cpp:buffer_bounds",
            severity=Severity.CRITICAL,
            category=FindingCategory.BUFFER_BOUNDS,
            file="native/core/vector.cpp",
            line=100,
            message="buffer overflow possible",
            tool="clang-tidy",
        )
        defaults.update(kwargs)
        return StaticFinding(**defaults)

    def test_add_and_contains(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        finding = self._make_finding()
        entry = BaselineEntry(
            finding=finding,
            added_at="2024-01-01T00:00:00Z",
            added_by="test",
            reason="historical violation",
        )
        baseline.add(entry)
        assert baseline.contains(finding)
        assert baseline.count() == 1

    def test_remove(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        finding = self._make_finding()
        entry = BaselineEntry(
            finding=finding,
            added_at="2024-01-01T00:00:00Z",
            added_by="test",
            reason="historical violation",
        )
        baseline.add(entry)
        assert baseline.remove(finding)
        assert not baseline.contains(finding)
        assert baseline.count() == 0

    def test_no_duplicate(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        finding = self._make_finding()
        entry = BaselineEntry(
            finding=finding,
            added_at="2024-01-01T00:00:00Z",
            added_by="test",
            reason="historical violation",
        )
        baseline.add(entry)
        baseline.add(entry)  # duplicate
        assert baseline.count() == 1

    def test_count_by_severity(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        for i in range(3):
            f = self._make_finding(line=i)
            baseline.add(BaselineEntry(
                finding=f, added_at="2024-01-01", added_by="test", reason="test",
            ))
        counts = baseline.count_by_severity()
        assert counts.get("CRITICAL") == 3

    def test_is_debt_increasing_new_finding(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        new_finding = self._make_finding()
        assert baseline.is_debt_increasing([new_finding])

    def test_is_debt_increasing_existing_finding(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        finding = self._make_finding()
        baseline.add(BaselineEntry(
            finding=finding, added_at="2024-01-01", added_by="test", reason="test",
        ))
        assert not baseline.is_debt_increasing([finding])

    def test_is_debt_increasing_empty(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        assert not baseline.is_debt_increasing([])

    def test_all_entries(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        finding = self._make_finding()
        entry = BaselineEntry(
            finding=finding,
            added_at="2024-01-01T00:00:00Z",
            added_by="test",
            reason="historical",
            expected_fix_version="2.0",
        )
        baseline.add(entry)
        entries = baseline.all_entries()
        assert len(entries) == 1
        assert entries[0].reason == "historical"
        assert entries[0].expected_fix_version == "2.0"


# ---------------------------------------------------------------------------
# Check Tiers
# ---------------------------------------------------------------------------

class TestCheckTiers:
    def test_four_tiers(self):
        assert len(CheckTier) == 4
        assert CheckTier.SAVE.value == "SAVE"
        assert CheckTier.PRECOMMIT.value == "PRECOMMIT"
        assert CheckTier.INTEGRATION.value == "INTEGRATION"
        assert CheckTier.RELEASE.value == "RELEASE"

    def test_save_scope(self):
        assert TIER_SCOPE[CheckTier.SAVE] == "affected-file"

    def test_precommit_scope(self):
        assert TIER_SCOPE[CheckTier.PRECOMMIT] == "affected-module"

    def test_integration_scope(self):
        assert "full" in TIER_SCOPE[CheckTier.INTEGRATION]

    def test_release_scope(self):
        assert "baseline" in TIER_SCOPE[CheckTier.RELEASE]


# ---------------------------------------------------------------------------
# Gate Evidence
# ---------------------------------------------------------------------------

class TestGateEvidence:
    def _make_finding(self, severity=Severity.CRITICAL, blocking=True, **kwargs):
        defaults = dict(
            language="cpp",
            rule="cpp:integer_overflow",
            severity=severity,
            category=FindingCategory.INTEGER_OVERFLOW,
            file="native/core/parser.cpp",
            line=42,
            message="integer overflow",
            tool="clang-tidy",
            is_blocking=blocking,
            codex_basis="A204" if blocking else "",
        )
        defaults.update(kwargs)
        return StaticFinding(**defaults)

    def test_compute_verdict_pass(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        findings = [self._make_finding(severity=Severity.LOW, blocking=False)]
        verdict, blocking, warning, new_debt = compute_verdict(findings, baseline)
        assert verdict == "PASS"
        assert blocking == 0

    def test_compute_verdict_warn(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        findings = [self._make_finding(severity=Severity.MEDIUM, blocking=False)]
        verdict, blocking, warning, new_debt = compute_verdict(findings, baseline)
        assert verdict == "WARN"
        assert warning == 1

    def test_compute_verdict_fail(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        findings = [self._make_finding(severity=Severity.CRITICAL, blocking=True)]
        verdict, blocking, warning, new_debt = compute_verdict(findings, baseline)
        assert verdict == "FAIL"
        assert blocking == 1
        assert new_debt == 1

    def test_compute_verdict_baseline_pass(self, tmp_path):
        """Historical violation in baseline doesn't block."""
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        finding = self._make_finding(severity=Severity.CRITICAL, blocking=True)
        baseline.add(BaselineEntry(
            finding=finding, added_at="2024-01-01", added_by="test", reason="historical",
        ))
        verdict, blocking, warning, new_debt = compute_verdict([finding], baseline)
        assert verdict == "PASS"
        assert blocking == 0
        assert new_debt == 0

    def test_run_static_analysis(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        findings = [self._make_finding(severity=Severity.LOW, blocking=False)]
        evidence = run_static_analysis(findings, baseline, CheckTier.SAVE)
        assert isinstance(evidence, StaticAnalysisGateEvidence)
        assert evidence.tier == CheckTier.SAVE
        assert evidence.verdict == "PASS"

    def test_gate_evidence_to_dict(self, tmp_path):
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        findings = [self._make_finding(severity=Severity.LOW, blocking=False)]
        evidence = run_static_analysis(findings, baseline, CheckTier.SAVE)
        d = evidence.to_dict()
        assert "tier" in d
        assert "verdict" in d
        assert "findings" in d

    def test_gate_evidence_to_gate_evidence_format(self, tmp_path):
        """Integrates with existing GateEvidence/Final Gate."""
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        findings = [self._make_finding(severity=Severity.CRITICAL, blocking=True)]
        evidence = run_static_analysis(findings, baseline, CheckTier.PRECOMMIT)
        gate = evidence.to_gate_evidence()
        assert gate["gate_type"] == "static_analysis"
        assert gate["verdict"] == "FAIL"
        assert "blocking_findings" in gate
        assert "summary" in gate
        assert gate["summary"]["total"] == 1

    def test_no_second_governance_verdict(self, tmp_path):
        """Gate evidence feeds existing gate — no second governance verdict."""
        baseline = LegacyBaseline(tmp_path / "baseline.json")
        findings = []
        evidence = run_static_analysis(findings, baseline, CheckTier.SAVE)
        gate = evidence.to_gate_evidence()
        # The gate evidence has a verdict, but it's fed INTO the existing
        # gate infrastructure — it doesn't create a second verdict.
        assert "gate_type" in gate
        assert gate["gate_type"] == "static_analysis"


# ---------------------------------------------------------------------------
# Auto-fix Policy
# ---------------------------------------------------------------------------

class TestAutoFixPolicy:
    def test_formatting_auto_fixable(self):
        assert is_auto_fixable(FindingCategory.FORMATTING)

    def test_integer_overflow_not_auto_fixable(self):
        assert not is_auto_fixable(FindingCategory.INTEGER_OVERFLOW)

    def test_buffer_bounds_not_auto_fixable(self):
        assert not is_auto_fixable(FindingCategory.BUFFER_BOUNDS)

    def test_lifetime_not_auto_fixable(self):
        assert not is_auto_fixable(FindingCategory.LIFETIME_SAFETY)

    def test_exception_boundary_not_auto_fixable(self):
        assert not is_auto_fixable(FindingCategory.EXCEPTION_BOUNDARY)

    def test_type_safety_not_auto_fixable(self):
        assert not is_auto_fixable(FindingCategory.TYPE_SAFETY)

    def test_boundary_contract_not_auto_fixable(self):
        assert not is_auto_fixable(FindingCategory.BOUNDARY_CONTRACT)

    def test_interop_width_not_auto_fixable(self):
        assert not is_auto_fixable(FindingCategory.INTEROP_WIDTH)

    def test_parameterization_not_auto_fixable(self):
        assert not is_auto_fixable(FindingCategory.PARAMETERIZATION)

    def test_never_auto_fix_categories(self):
        """Semantic findings are never auto-fixed."""
        for cat in NEVER_AUTO_FIX_CATEGORIES:
            assert not is_auto_fixable(cat)


# ---------------------------------------------------------------------------
# Codex Basis Validation
# ---------------------------------------------------------------------------

class TestCodexBasis:
    def test_blocking_with_codex_basis(self):
        f = StaticFinding(
            language="cpp", rule="cpp:overflow", severity=Severity.CRITICAL,
            category=FindingCategory.INTEGER_OVERFLOW, file="f.cpp", line=1,
            message="overflow", tool="clang-tidy",
            codex_basis="A204", is_blocking=True,
        )
        assert validate_codex_basis(f)

    def test_blocking_without_codex_basis(self):
        f = StaticFinding(
            language="cpp", rule="cpp:overflow", severity=Severity.CRITICAL,
            category=FindingCategory.INTEGER_OVERFLOW, file="f.cpp", line=1,
            message="overflow", tool="clang-tidy",
            codex_basis="", is_blocking=True,
        )
        assert not validate_codex_basis(f)

    def test_non_blocking_no_codex_required(self):
        f = StaticFinding(
            language="cpp", rule="cpp:style", severity=Severity.LOW,
            category=FindingCategory.STYLE, file="f.cpp", line=1,
            message="style issue", tool="clang-tidy",
            codex_basis="", is_blocking=False,
        )
        assert validate_codex_basis(f)

    def test_filter_third_party_defaults(self):
        """Third-party analyzer defaults without Codex basis are downgraded."""
        findings = [
            StaticFinding(
                language="python", rule="pylint:missing-docstring",
                severity=Severity.HIGH, category=FindingCategory.STYLE,
                file="f.py", line=1, message="missing docstring",
                tool="pylint", codex_basis="", is_blocking=True,
            ),
            StaticFinding(
                language="cpp", rule="cpp:overflow", severity=Severity.CRITICAL,
                category=FindingCategory.INTEGER_OVERFLOW, file="f.cpp", line=1,
                message="overflow", tool="clang-tidy",
                codex_basis="A204", is_blocking=True,
            ),
        ]
        filtered = filter_third_party_defaults(findings)
        # First finding (no Codex basis) is downgraded to non-blocking
        assert not filtered[0].is_blocking
        # Second finding (with Codex basis) stays blocking
        assert filtered[1].is_blocking

    def test_third_party_defaults_not_policy(self):
        """Third-party analyzer defaults do NOT become Codex policy."""
        f = StaticFinding(
            language="typescript", rule="eslint:prefer-const",
            severity=Severity.MEDIUM, category=FindingCategory.STYLE,
            file="f.ts", line=1, message="prefer const",
            tool="eslint", codex_basis="", is_blocking=True,
        )
        assert not validate_codex_basis(f)
        filtered = filter_third_party_defaults([f])
        assert not filtered[0].is_blocking


# ---------------------------------------------------------------------------
# Language Baselines
# ---------------------------------------------------------------------------

class TestLanguageBaselines:
    def test_c_baseline(self):
        bl = get_language_baseline("c")
        assert bl is not None
        assert bl.standard == LanguageStandard.C
        assert "/std:c17" in bl.compiler_flags

    def test_cpp_baseline(self):
        bl = get_language_baseline("cpp")
        assert bl is not None
        assert bl.standard == LanguageStandard.CPP
        assert "/std:c++20" in bl.compiler_flags

    def test_python_baseline(self):
        bl = get_language_baseline("python")
        assert bl is not None
        assert bl.standard == LanguageStandard.PYTHON
        assert bl.linter_config.get("strict_typing_boundary") is True

    def test_typescript_baseline(self):
        bl = get_language_baseline("typescript")
        assert bl is not None
        assert bl.standard == LanguageStandard.TYPESCRIPT
        assert bl.linter_config.get("strict") is True
        assert bl.linter_config.get("strictNullChecks") is True
        assert bl.linter_config.get("noImplicitAny") is True
        assert bl.linter_config.get("noImplicitReturns") is True
        assert bl.linter_config.get("noFallthroughCasesInSwitch") is True

    def test_typescript_phased(self):
        bl = get_language_baseline("typescript")
        assert bl.linter_config.get("noUncheckedIndexedAccess") == "phased"
        assert bl.linter_config.get("exactOptionalPropertyTypes") == "phased"

    def test_csharp_baseline(self):
        bl = get_language_baseline("csharp")
        assert bl is not None
        assert bl.standard == LanguageStandard.CSHARP
        assert bl.linter_config.get("nullable_analysis") is True
        assert bl.linter_config.get("interop_width_check") is True

    def test_sql_baseline(self):
        bl = get_language_baseline("sql")
        assert bl is not None
        assert bl.standard == LanguageStandard.SQL
        assert bl.linter_config.get("parameterization_check") is True

    def test_all_baselines(self):
        baselines = all_language_baselines()
        assert len(baselines) == 6
        assert "c" in baselines
        assert "cpp" in baselines
        assert "python" in baselines
        assert "typescript" in baselines
        assert "csharp" in baselines
        assert "sql" in baselines

    def test_unknown_language(self):
        assert get_language_baseline("rust") is None
