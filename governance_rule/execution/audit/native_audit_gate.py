"""Native audit-engine gate for the commit hook (P0-9, §1.1).

Shadow integration of ``native/audit/audit_engine.cpp`` into the
pre-commit audit flow:

- the C++ engine executes every manifest check it supports natively;
  its verdict is **authoritative** — a FAIL blocks the commit exactly
  like a Python check error;
- engine timeout is fail-closed (A537 test/audit budget);
- an unavailable engine (exe absent and not buildable in this
  environment) is recorded as ``delegated`` — the Python oracle still
  covers the same governed files, so transition coverage is not lost;
- cache invalidation: when the governed codex database is newer than
  the manifest, the manifest is regenerated before the engine runs.

The engine is read-only: it never modifies audited files.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ENGINE_EXE_RELATIVE = Path("native") / "test_suites" / "bin" / "audit-engine.exe"
MANIFEST_RELATIVE = (
    Path("governance_rule") / "execution" / "audit"
    / "audit_checks_manifest.json"
)
CODEX_DB_RELATIVE = (
    Path("governance_rule") / "codex" / "data" / "governance_codex.sqlite3"
)
REPORT_RELATIVE = (
    Path("native") / "test_suites" / "bin" / "audit-report.json"
)

# Audit budget: the whole commit gate must stay ≤30 s (A537); the native
# engine is bounded well below that so the Python oracle still has room.
ENGINE_TIMEOUT_S = float(os.environ.get("GPTBRIDGE_AUDIT_ENGINE_TIMEOUT", "25"))


@dataclass
class NativeAuditResult:
    status: str  # "pass" | "fail" | "delegated" | "timeout"
    elapsed_s: float = 0.0
    passed: int = 0
    failed: int = 0
    delegated: int = 0
    errors: list[str] = field(default_factory=list)
    note: str = ""

    def summary(self) -> str:
        if self.status == "delegated":
            return f"native audit engine delegated: {self.note}"
        if self.status == "timeout":
            return (
                f"native audit engine timeout after {self.elapsed_s:.2f}s "
                f"(fail-closed)"
            )
        return (
            f"native audit engine: {self.passed} pass / {self.failed} fail / "
            f"{self.delegated} delegated in {self.elapsed_s:.2f}s"
        )


def _find_vcvars() -> Path | None:
    """Locate vcvars64.bat for an on-demand single-file engine build."""
    roots = [
        Path(r"E:\Program Files\Microsoft Visual Studio\18\Community"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools"),
    ]
    for root in roots:
        candidate = (
            root / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
        )
        if candidate.is_file():
            return candidate
    return None


def _build_engine(root: Path) -> bool:
    """Single-TU build of audit-engine.exe (bounded; no Python, no full
    suite rebuild). Returns False when the toolchain is unavailable."""
    vcvars = _find_vcvars()
    if vcvars is None:
        return False
    exe = root / ENGINE_EXE_RELATIVE
    exe.parent.mkdir(parents=True, exist_ok=True)
    src = root / "native" / "audit" / "audit_engine.cpp"
    include = root / "native" / "include"
    bat = (
        f'@echo off\r\ncall "{vcvars}" >nul || exit /b 1\r\n'
        f'cl /nologo /std:c++17 /utf-8 /O2 /EHsc '
        f'/DGPTBRIDGE_AUDIT_ENGINE_CLI /I"{include}" '
        f'/Fe:"{exe}" /Fo:"{exe.parent}\\\\" "{src}" >nul || exit /b 1\r\n'
    )
    bat_path = exe.parent / "_audit_engine_build.bat"
    bat_path.write_text(bat, encoding="ascii")
    try:
        result = subprocess.run(
            ["cmd", "/c", str(bat_path)],
            cwd=root,
            capture_output=True,
            timeout=120,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and exe.is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _refresh_manifest_if_stale(root: Path) -> str | None:
    """Regenerate the manifest when the governed codex is newer
    (法典變更 → cache invalidation, P0-9 ④).  Returns an error string on
    failure, None on success/no-op."""
    manifest = root / MANIFEST_RELATIVE
    codex = root / CODEX_DB_RELATIVE
    if not manifest.is_file() or (
        codex.is_file()
        and codex.stat().st_mtime > manifest.stat().st_mtime
    ):
        try:
            from governance_rule.execution.audit.export_audit_manifest import (
                build_manifest,
            )
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    build_manifest(root), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
        except Exception as error:  # noqa: BLE001 — fail-visible, not silent
            return f"manifest refresh failed: {error}"
    return None


def run_native_audit_gate(root: Path) -> NativeAuditResult:
    """Execute the native audit engine against the governed manifest."""
    exe = root / ENGINE_EXE_RELATIVE
    if not exe.is_file():
        if not _build_engine(root):
            return NativeAuditResult(
                status="delegated",
                note="audit-engine.exe unavailable; python oracle covers",
            )
    refresh_error = _refresh_manifest_if_stale(root)
    if refresh_error:
        return NativeAuditResult(status="fail", errors=[refresh_error])
    manifest = root / MANIFEST_RELATIVE
    report_path = root / REPORT_RELATIVE
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [
                str(exe),
                "--manifest", str(manifest),
                "--root", str(root),
                "--report", str(report_path),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ENGINE_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return NativeAuditResult(
            status="timeout",
            elapsed_s=time.monotonic() - started,
            errors=[f"native audit engine exceeded {ENGINE_TIMEOUT_S}s"],
        )
    except OSError as error:
        return NativeAuditResult(
            status="delegated",
            note=f"engine spawn failed ({error}); python oracle covers",
        )
    elapsed = time.monotonic() - started
    result = NativeAuditResult(
        status="pass" if proc.returncode == 0 else "fail",
        elapsed_s=elapsed,
    )
    try:
        report = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        report = {}
    result.passed = int(report.get("passed", 0) or 0)
    result.failed = int(report.get("failed", 0) or 0)
    result.delegated = int(report.get("delegated", 0) or 0)
    if not report.get("manifest_ok", True):
        result.status = "fail"
        result.errors.append(
            f"manifest error: {report.get('manifest_error', 'unknown')}")
    for check in report.get("checks", []):
        if check.get("status") == "FAIL":
            result.errors.append(
                f"{check.get('id', '?')}: {check.get('detail', '')}")
    if proc.returncode != 0 and not result.errors:
        result.errors.append(
            f"audit engine exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:200]}"
        )
    return result


if __name__ == "__main__":
    outcome = run_native_audit_gate(Path.cwd())
    print(outcome.summary())
    for error in outcome.errors:
        print(f"[FAIL] {error}", file=sys.stderr)
    raise SystemExit(0 if outcome.status in ("pass", "delegated") else 1)
