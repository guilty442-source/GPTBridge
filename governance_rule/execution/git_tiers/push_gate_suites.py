"""Push-gate native-suite machinery (A185 split).

Extracted from ``push_gate.py`` (source-size contract): canonical
``native/test_suites`` layout constants, suite-binary staleness checks
(mtime link inputs + G99 manifest content binding), the canonical
``build.ps1`` rebuild path and the §10.60.1 C# TestSuiteOrchestrator
resolution/invocation/report parsing.  ``mandatory_test_gate`` in
``push_gate`` composes these primitives.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from . import branch_policy

# Canonical native suite locations (single source: native/test_suites).
_NATIVE_TEST_DIR = Path("native") / "test_suites"
_BUILD_SCRIPT = _NATIVE_TEST_DIR / "build.ps1"
_BIN_DIR = _NATIVE_TEST_DIR / "bin"
_SUITE_GLOB = "*_suite.exe"
_SUITE_MANIFEST_NAME = "suite-manifest.json"

# G97: the C# TestSuiteOrchestrator is the SOLE native suite orchestrator
# (§10.60.1).  The gate resolves the built executable (Release first),
# rebuilds it from source when missing/stale, and consumes its typed
# orchestration report instead of spawning suite processes itself.
_ORCH_DIR = _NATIVE_TEST_DIR / "csharp"
_ORCH_PROJECT = _ORCH_DIR / "TestSuiteOrchestrator.csproj"
_ORCH_SOURCE = _ORCH_DIR / "Program.cs"
_ORCH_EXE_RELEASE = (
    _ORCH_DIR / "bin" / "Release" / "net10.0" / "TestSuiteOrchestrator.exe"
)
_ORCH_EXE_DEBUG = (
    _ORCH_DIR / "bin" / "Debug" / "net10.0" / "TestSuiteOrchestrator.exe"
)
_ORCH_REPORT_NAME = "native-orchestration-report.json"

# Link inputs that invalidate cached suite binaries (build.ps1 $suites map
# roots): a newer code file in any of these means the binaries no longer
# test the tree that would be pushed.
_DEP_ROOTS = (
    _NATIVE_TEST_DIR,
    Path("native") / "core",
    Path("native") / "include",
    Path("native") / "tool_runtime",
    Path("native") / "audit",
    Path("Standalone tools")
    / branch_policy.LOCAL_MODEL_BRANCH
    / "src"
    / "backend"
    / "cpp",
)
_CODE_SUFFIXES = frozenset({".c", ".cpp", ".h", ".hpp"})

Runner = Callable[..., Any]
Builder = Callable[..., Any]


def _suite_exes(bin_dir: Path) -> list[Path]:
    """Suite binaries; ``_``-prefixed helpers are not suites."""
    try:
        return sorted(
            p for p in bin_dir.glob(_SUITE_GLOB)
            if p.is_file() and not p.name.startswith("_")
        )
    except OSError:
        return []


def _newest_source_mtime(root: Path) -> float:
    """Newest mtime across the suite link inputs (0 when none found)."""
    newest = 0.0
    for rel in _DEP_ROOTS:
        dep_root = Path(root) / rel
        if not dep_root.is_dir():
            continue
        try:
            for path in dep_root.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in _CODE_SUFFIXES
                ):
                    newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _binaries_stale(root: Path, exes: list[Path]) -> bool:
    """True when any link input is newer than the oldest suite binary."""
    if not exes:
        return True
    oldest = min(exe.stat().st_mtime for exe in exes)
    return _newest_source_mtime(root) > oldest


def _suite_artifacts_stale(
    bin_dir: Path, manifest: Mapping[str, Any]
) -> list[str]:
    """Content-binding check (G99): every manifest suite row records the
    exe SHA-256 captured at build time.  A binary whose content differs —
    partial rebuild, swapped exe, drifted manifest — is stale regardless
    of timestamps; a row with no recorded hash is unverifiable and counts
    as stale too."""
    suites = manifest.get("suites")
    if not isinstance(suites, list) or not suites:
        return []
    stale: list[str] = []
    for row in suites:
        if not isinstance(row, Mapping):
            continue
        exe = str(row.get("exe") or "")
        expected = str(row.get("sha256") or "").lower()
        if not exe:
            continue
        path = bin_dir / exe
        try:
            import hashlib

            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            actual = ""
        if not expected or actual != expected:
            stale.append(str(row.get("name") or exe))
    return stale


def _powershell() -> str:
    return os.environ.get("GPTBRIDGE_POWERSHELL", "powershell.exe")


def _build_suites(root: Path, timeout_s: float, builder: Builder | None) -> Any:
    """One bounded rebuild via the canonical build.ps1 (returns proc)."""
    script = Path(root) / _BUILD_SCRIPT
    run = builder or (
        lambda timeout: subprocess.run(
            [
                _powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script),
            ],
            cwd=Path(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    )
    return run(timeout_s)


def _orchestrator_exe(root: Path) -> Path | None:
    """The built C# orchestrator executable (Release preferred)."""
    for rel in (_ORCH_EXE_RELEASE, _ORCH_EXE_DEBUG):
        exe = root / rel
        if exe.is_file():
            return exe
    return None


def _orchestrator_stale(root: Path, exe: Path) -> bool:
    """Rebuild when the orchestrator sources are newer than the exe."""
    try:
        exe_mtime = exe.stat().st_mtime
        for rel in (_ORCH_SOURCE, _ORCH_PROJECT):
            src = root / rel
            if src.is_file() and src.stat().st_mtime > exe_mtime:
                return True
    except OSError:
        return True
    return False


def _ensure_orchestrator(
    root: Path, timeout_s: float
) -> Path | None:
    """Resolve the orchestrator exe, rebuilding via ``dotnet build`` when
    missing or stale.  Returns ``None`` when unavailable — fail closed."""
    exe = _orchestrator_exe(root)
    if exe is not None and not _orchestrator_stale(root, exe):
        return exe
    try:
        proc = subprocess.run(
            [
                "dotnet", "build", str(root / _ORCH_PROJECT),
                "-c", "Release", "--nologo", "-v", "q",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _orchestrator_exe(root)


def _default_orchestrator(argv: list[str], cwd: Path, timeout_s: float) -> Any:
    """Invoke the C# orchestrator.  Output goes to a log file, not a pipe:
    suite processes may spawn grandchildren that inherit a pipe and keep
    it open past the parent's exit — a captured pipe could then never
    reach EOF and would hang the gate past its budget."""
    log_path = Path(cwd) / "_gate_orchestrator.log"
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        return subprocess.run(
            argv,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _orchestration_report(bin_dir: Path) -> dict[str, Any]:
    """Parse the orchestrator's ``native-orchestration-report.json``."""
    try:
        data = json.loads(
            (bin_dir / _ORCH_REPORT_NAME).read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _suite_manifest(bin_dir: Path) -> dict[str, Any]:
    """Parse ``bin/suite-manifest.json`` (emitted by build.ps1 with the
    built source revision).  Missing/malformed → ``{}``; callers treat an
    absent manifest as stale build output (G99 revision binding)."""
    try:
        data = json.loads(
            (bin_dir / _SUITE_MANIFEST_NAME).read_text(
                encoding="utf-8-sig"
            )
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "Builder", "Runner",
    "_BIN_DIR", "_BUILD_SCRIPT", "_CODE_SUFFIXES", "_DEP_ROOTS",
    "_NATIVE_TEST_DIR", "_ORCH_DIR", "_ORCH_EXE_DEBUG",
    "_ORCH_EXE_RELEASE", "_ORCH_PROJECT", "_ORCH_REPORT_NAME",
    "_ORCH_SOURCE", "_SUITE_GLOB", "_SUITE_MANIFEST_NAME",
    "_binaries_stale", "_build_suites", "_default_orchestrator",
    "_ensure_orchestrator", "_newest_source_mtime",
    "_orchestration_report", "_orchestrator_exe", "_orchestrator_stale",
    "_powershell", "_suite_artifacts_stale", "_suite_exes",
    "_suite_manifest",
]
