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
- cache invalidation: when the governed codex authority is newer than
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
from typing import Any

ENGINE_EXE_RELATIVE = Path("native") / "test_suites" / "bin" / "audit-engine.exe"
MANIFEST_RELATIVE = (
    Path("governance_rule") / "execution" / "audit"
    / "audit_checks_manifest.json"
)
REPORT_RELATIVE = (
    Path("native") / "test_suites" / "bin" / "audit-report.json"
)

# Audit budget: the whole commit gate must stay ≤30 s (A537); the native
# engine plus the same-request delegated lane share it (G96).
ENGINE_TIMEOUT_S = float(os.environ.get("GPTBRIDGE_AUDIT_ENGINE_TIMEOUT", "25"))


@dataclass
class NativeAuditResult:
    status: str  # "pass" | "fail" | "delegated" | "timeout"
    elapsed_s: float = 0.0
    passed: int = 0
    failed: int = 0
    delegated: int = 0
    delegated_executed: int = 0
    errors: list[str] = field(default_factory=list)
    note: str = ""
    provenance: dict = field(default_factory=dict)

    def summary(self) -> str:
        if self.status == "delegated":
            return f"native audit engine delegated: {self.note}"
        if self.status == "timeout":
            return (
                f"native audit engine timeout after {self.elapsed_s:.2f}s "
                f"(fail-closed)"
            )
        executed = (
            f" / {self.delegated_executed} python-delegated executed"
            if self.delegated_executed
            else ""
        )
        return (
            f"native audit engine: {self.passed} pass / {self.failed} fail / "
            f"{self.delegated} delegated{executed} in {self.elapsed_s:.2f}s"
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
        ok = result.returncode == 0 and exe.is_file()
        if ok:
            _write_engine_digest(root, exe)
        return ok
    except (OSError, subprocess.TimeoutExpired):
        return False


def _engine_deps(root: Path) -> tuple[Path, ...]:
    return (
        root / "native" / "audit" / "audit_engine.cpp",
        root / "native" / "include" / "audit_engine.h",
    )


def _digest_path(exe: Path) -> Path:
    return exe.parent / (exe.name + ".sha256")


def _write_engine_digest(root: Path, exe: Path) -> None:
    """Record sha256 of the source deps at build time so staleness checks
    are not mtime-only (a same-mtime forged source would otherwise slip)."""
    import hashlib

    digest = hashlib.sha256()
    for dep in _engine_deps(root):
        digest.update(dep.name.encode())
        digest.update(dep.read_bytes())
    _digest_path(exe).write_text(digest.hexdigest(), encoding="ascii")


def _engine_stale(root: Path, exe: Path) -> bool:
    """Engine binary cache invalidation: the cached exe must be rebuilt
    when the single-TU source or its public header is newer, otherwise a
    stale binary would keep executing superseded check logic.  A recorded
    source digest mismatch also forces rebuild (mtime alone is forgeable)."""
    exe_mtime = exe.stat().st_mtime
    for dep in _engine_deps(root):
        if dep.is_file() and dep.stat().st_mtime > exe_mtime:
            return True
    import hashlib

    digest = hashlib.sha256()
    for dep in _engine_deps(root):
        digest.update(dep.name.encode())
        digest.update(dep.read_bytes())
    try:
        recorded = _digest_path(exe).read_text(encoding="ascii").strip()
    except OSError:
        return True
    return recorded != digest.hexdigest()


def _codex_authority_mtime() -> float:
    """Epoch timestamp of the live PostgreSQL codex authority import.

    Raises when the authority cannot be probed — the caller treats that
    as stale (fail-closed regeneration), never as fresh.
    """
    from governance_rule.execution.codex_postgresql import authority_state

    state = authority_state()
    imported_at = state.get("imported_at")
    if hasattr(imported_at, "timestamp"):
        return float(imported_at.timestamp())
    raise RuntimeError("codex authority state lacks imported_at")


def _refresh_manifest_if_stale(root: Path) -> str | None:
    """Regenerate the manifest when the governed codex or any audit check
    module is newer (cache invalidation, G96 manifest-generation parity).
    Returns an error string on failure, None on success/no-op."""
    manifest = root / MANIFEST_RELATIVE
    stale = not manifest.is_file()
    if not stale:
        manifest_mtime = manifest.stat().st_mtime
        try:
            codex_mtime = _codex_authority_mtime()
        except Exception:
            # Authority unreadable: fail closed into regeneration rather
            # than trusting a manifest built against an unknown codex.
            stale = True
        else:
            stale = codex_mtime > manifest_mtime
        if not stale:
            # Check-module edits change the delegated set even when the
            # codex is untouched — regenerate on source drift too.
            audit_dir = root / "governance_rule" / "execution" / "audit"
            for module in audit_dir.glob("audit_*.py"):
                if module.stat().st_mtime > manifest_mtime:
                    stale = True
                    break
    if stale:
        try:
            from governance_rule.execution.audit.export_audit_manifest import (
                build_manifest,
            )
            from governance_rule.execution.codex_update_pipeline import (
                _set_read_only,
            )
            manifest.parent.mkdir(parents=True, exist_ok=True)
            # The manifest is itself a protected readonly source: lift the
            # attribute for the governed regeneration, restore afterwards.
            from governance_rule.execution.git_tiers.protected_attrs import (
                _is_read_only,
            )
            was_readonly = manifest.is_file() and _is_read_only(manifest)
            if was_readonly:
                _set_read_only(manifest, False)
            try:
                manifest.write_text(
                    json.dumps(
                        build_manifest(root), ensure_ascii=False, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
            finally:
                if was_readonly:
                    _set_read_only(manifest, True)
        except Exception as error:  # noqa: BLE001 — fail-visible, not silent
            return f"manifest refresh failed: {error}"
    return None


def run_native_audit_gate(root: Path) -> NativeAuditResult:
    """Execute the native audit engine against the governed manifest."""
    exe = root / ENGINE_EXE_RELATIVE
    if not exe.is_file() or _engine_stale(root, exe):
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
    # Evidence reconciliation: the report must account for every manifest
    # check id exactly once — a truncated/duplicated/foreign report row
    # means a required check was never executed and must not silent-pass.
    # A DELEGATED verdict is a handoff, not a pass — every engine-delegated
    # id must name a manifest ``delegated`` row so the Python lane actually
    # executes it; an unknown/typo'd kind otherwise delegates to nobody.
    try:
        manifest_doc = json.loads(manifest.read_text(encoding="utf-8-sig"))
        manifest_checks = manifest_doc.get("checks", [])
        manifest_ids = {str(c.get("id", "")) for c in manifest_checks}
        manifest_delegated = {
            str(c.get("id", ""))
            for c in manifest_checks
            if c.get("kind") == "delegated"
        }
        if len(manifest_ids) != len(manifest_checks):
            result.errors.append("manifest contains duplicate check ids")
    except (OSError, json.JSONDecodeError):
        manifest_ids = set()
        manifest_delegated = set()
        result.errors.append("manifest unreadable during report reconciliation")
    report_checks = report.get("checks", [])
    report_ids = [str(c.get("id", "")) for c in report_checks]
    report_id_set = set(report_ids)
    if len(report_id_set) != len(report_ids):
        seen: set[str] = set()
        dupes = {i for i in report_ids if i in seen or seen.add(i)}
        result.errors.append(
            f"report contains duplicate result rows: {sorted(dupes)[:5]}")
    missing = manifest_ids - report_id_set
    if missing:
        result.errors.append(
            f"report missing results for {len(missing)} manifest checks: "
            f"{sorted(missing)[:5]}")
    foreign = report_id_set - manifest_ids
    if foreign:
        result.errors.append(
            f"report contains {len(foreign)} ids absent from manifest: "
            f"{sorted(foreign)[:5]}")
    for check in report_checks:
        status = check.get("status")
        if status == "FAIL":
            result.errors.append(
                f"{check.get('id', '?')}: {check.get('detail', '')}")
        elif status == "DELEGATED":
            if str(check.get("id", "")) not in manifest_delegated:
                result.errors.append(
                    f"{check.get('id', '?')}: engine-delegated without "
                    "manifest delegated row — check would never execute"
                )
        elif status != "PASS":
            # Engine vocabulary is PASS/FAIL/DELEGATED only; BLOCKED,
            # TIMEOUT, STALE or any other row means corrupted evidence.
            result.errors.append(
                f"{check.get('id', '?')}: unknown report status "
                f"{status!r} — evidence not trustworthy")
    if result.errors:
        result.status = "fail"
    # Artifact freshness provenance (G96/G99): bind this verdict to the
    # exact binary, manifest content, source revision and suite evidence
    # that produced it — content hashes, never file timestamps alone.
    provenance: dict[str, Any] = {}
    try:
        import hashlib as _hashlib

        provenance["engine_sha256"] = _hashlib.sha256(
            exe.read_bytes()
        ).hexdigest()
        provenance["manifest_sha256"] = _hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest()
        try:
            git = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            provenance["source_revision"] = (
                git.stdout.strip() if git.returncode == 0 else None
            )
        except (OSError, subprocess.TimeoutExpired):
            provenance["source_revision"] = None
        bin_dir = root / "native" / "test_suites" / "bin"
        suite_manifest = bin_dir / "suite-manifest.json"
        if suite_manifest.is_file():
            provenance["suite_manifest_sha256"] = _hashlib.sha256(
                suite_manifest.read_bytes()
            ).hexdigest()
            try:
                suite_doc = json.loads(
                    suite_manifest.read_text(encoding="utf-8-sig"))
                provenance["suite_manifest_revision"] = suite_doc.get(
                    "revision")
            except (OSError, json.JSONDecodeError):
                provenance["suite_manifest_revision"] = None
        orch_report = bin_dir / "native-orchestration-report.json"
        if orch_report.is_file():
            try:
                orch_doc = json.loads(
                    orch_report.read_text(encoding="utf-8-sig"))
                provenance["suite_artifact_hash"] = orch_doc.get(
                    "artifact_hash")
                provenance["suite_verdict"] = orch_doc.get("verdict")
            except (OSError, json.JSONDecodeError):
                provenance["suite_artifact_hash"] = None
        result.provenance = provenance
        # Persist provenance into the on-disk report so the evidence file
        # itself carries the binding (stdout copy stays engine-pure).
        try:
            report_doc = json.loads(
                report_path.read_text(encoding="utf-8-sig"))
            report_doc["provenance"] = provenance
            report_path.write_text(
                json.dumps(report_doc, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (OSError, json.JSONDecodeError) as error:
            result.errors.append(
                f"audit report provenance write failed: {error}")
            result.status = "fail"
    except OSError as error:
        result.errors.append(f"provenance hashing failed: {error}")
        result.status = "fail"
    if proc.returncode != 0 and not result.errors:
        result.errors.append(
            f"audit engine exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:200]}"
        )
    return result


def run_audit_request(root: Path) -> NativeAuditResult:
    """Canonical audit request (G96): manifest → C++ engine → delegated
    Python checks → merged verdict inside one request and one budget.

    - engine FAIL/timeout is fail-closed and ends the request;
    - an unavailable engine falls back to the full Python oracle (the
      documented coverage path) rather than auditing nothing;
    - on engine pass the manifest's delegated checks execute in Python
      under the same request — an unfinished delegated set can never
      produce a PASS;
    - engine + delegated together must fit the A537 30 s audit budget.
    """
    started = time.monotonic()
    result = run_native_audit_gate(root)
    if result.status in ("fail", "timeout"):
        return result
    if result.status == "delegated":
        from .audit_checks import audit_runtime_governance

        oracle_errors = audit_runtime_governance(
            root, include_self_health=False
        )
        if oracle_errors:
            result.status = "fail"
            result.errors.extend(oracle_errors)
        result.note = (result.note + "; full python oracle executed").strip(
            "; "
        )
    else:
        try:
            manifest = json.loads(
                (root / MANIFEST_RELATIVE).read_text(
                    encoding="utf-8-sig"
                )
            )
        except (OSError, json.JSONDecodeError) as error:
            result.status = "fail"
            result.errors.append(
                f"delegated manifest unreadable: {error}"
            )
        else:
            from .audit_checks import (
                AUDIT_FLOW_BUDGET_SECONDS,
                run_delegated_checks,
            )

            remaining = max(
                0.0, AUDIT_FLOW_BUDGET_SECONDS - result.elapsed_s
            )
            delegated_errors, executed = run_delegated_checks(
                root,
                manifest.get("checks", []),
                budget_seconds=remaining,
            )
            result.delegated_executed = len(executed)
            if delegated_errors:
                result.status = "fail"
                result.errors.extend(delegated_errors)
    from .audit_checks import audit_flow_budget_error

    budget_error = audit_flow_budget_error(time.monotonic() - started)
    if budget_error:
        result.status = "fail"
        result.errors.append(budget_error)
    return result


if __name__ == "__main__":
    outcome = run_audit_request(Path.cwd())
    print(outcome.summary())
    for error in outcome.errors:
        print(f"[FAIL] {error}", file=sys.stderr)
    raise SystemExit(0 if outcome.status in ("pass", "delegated") else 1)
