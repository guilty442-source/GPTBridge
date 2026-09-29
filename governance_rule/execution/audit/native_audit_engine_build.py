"""Native audit-engine build + staleness helpers (split from native_audit_gate)."""

from __future__ import annotations

import subprocess
from pathlib import Path


ENGINE_EXE_RELATIVE = Path("native") / "test_suites" / "bin" / "audit-engine.exe"


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
        f'cl /nologo /std:c++latest /utf-8 /O2 /EHsc '
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
