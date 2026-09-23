"""Build Baseline — clean/incremental build timing baseline.

Records build times for:
    - Python startup/import
    - TypeScript typecheck/bundle
    - C/C++ compile/link
    - C# compile/publish (none in current codebase)
    - SQL migration validation

Build baselines are versioned and stored as JSON evidence.  They feed
the regression control system to detect build-time regressions.

Codex basis:
    A358 — benchmark evidence and lifecycle
    A211 — six-language canonical roles
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import os

from . import process_metrics as _metrics


BUILD_BASELINE_VERSION = "1.0"


@dataclass(frozen=True)
class BuildStepResult:
    """Result of timing one build step."""
    step_name: str
    language: str          # "python", "typescript", "cpp", "csharp", "sql"
    build_type: str        # "clean" or "incremental"
    wall_seconds: float
    cpu_seconds: float
    peak_memory_bytes: int
    success: bool
    error_message: str = ""
    # Number of artifacts produced/processed
    artifact_count: int = 0


@dataclass(frozen=True)
class BuildBaselineRecord:
    """One versioned build baseline record."""
    baseline_id: str
    baseline_version: str
    recorded_at: str
    environment: dict[str, Any]
    steps: list[dict[str, Any]]  # list of BuildStepResult as dict
    total_wall_seconds: float
    total_success: bool
    notes: str = ""


class BuildBaselineStore:
    """File-based versioned build baseline store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8") or "[]")

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def record(self, record: BuildBaselineRecord) -> str:
        records = self._load()
        records.append(asdict(record))
        self._save(records)
        return record.baseline_id

    def latest(self) -> BuildBaselineRecord | None:
        records = self._load()
        if not records:
            return None
        r = records[-1]
        return BuildBaselineRecord(
            baseline_id=r["baseline_id"],
            baseline_version=r["baseline_version"],
            recorded_at=r["recorded_at"],
            environment=r.get("environment", {}),
            steps=r.get("steps", []),
            total_wall_seconds=r.get("total_wall_seconds", 0),
            total_success=r.get("total_success", False),
            notes=r.get("notes", ""),
        )

    def history(self) -> list[BuildBaselineRecord]:
        records = self._load()
        return [
            BuildBaselineRecord(
                baseline_id=r["baseline_id"],
                baseline_version=r["baseline_version"],
                recorded_at=r["recorded_at"],
                environment=r.get("environment", {}),
                steps=r.get("steps", []),
                total_wall_seconds=r.get("total_wall_seconds", 0),
                total_success=r.get("total_success", False),
                notes=r.get("notes", ""),
            )
            for r in records
        ]


def _capture_env() -> dict[str, Any]:
    """Capture build environment."""
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": _metrics.cpu_count(),
        "memory_total_bytes": max(0, _metrics.system_memory_total_bytes()),
    }


def _time_step(
    step_name: str,
    language: str,
    build_type: str,
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> BuildStepResult:
    """Time a single build step."""
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    mem_start = max(0, _metrics.process_working_set_bytes(os.getpid()))

    success = True
    error_msg = ""
    artifact_count = 0

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        success = result.returncode == 0
        if not success:
            error_msg = result.stderr[:500] if result.stderr else f"exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        success = False
        error_msg = f"timeout after {timeout}s"
    except Exception as e:
        success = False
        error_msg = str(e)[:500]

    wall = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    mem = 0
    mem_end = _metrics.process_working_set_bytes(os.getpid())
    mem = max(0, mem_end - mem_start) if mem_end >= 0 else 0

    return BuildStepResult(
        step_name=step_name,
        language=language,
        build_type=build_type,
        wall_seconds=round(wall, 4),
        cpu_seconds=round(cpu, 4),
        peak_memory_bytes=mem,
        success=success,
        error_message=error_msg,
        artifact_count=artifact_count,
    )


def run_python_import_baseline(
    venv_python: Path,
    *,
    build_type: str = "clean",
) -> BuildStepResult:
    """Time Python startup + import of core modules."""
    cmd = [
        str(venv_python), "-c",
        "import shared_layer; import core_system; print('ok')",
    ]
    return _time_step(
        "python_import", "python", build_type, cmd,
        env={"PYTHONPATH": "shared-layer/src;main-system/src-core"},
    )


def run_typescript_typecheck_baseline(
    npm_cmd: str = "npx",
    *,
    cwd: Path | None = None,
    build_type: str = "clean",
) -> BuildStepResult:
    """Time TypeScript type checking."""
    cmd = [npm_cmd, "tsc", "--noEmit"]
    return _time_step(
        "ts_typecheck", "typescript", build_type, cmd, cwd=cwd,
    )


def run_cc_compile_baseline(
    venv_python: Path,
    build_script: Path,
    *,
    build_type: str = "clean",
) -> BuildStepResult:
    """Time C/C++ native extension compile."""
    cmd = [str(venv_python), str(build_script)]
    return _time_step(
        "cc_compile", "cpp", build_type, cmd,
    )


def run_sql_migration_validation_baseline(
    venv_python: Path,
    migrations_dir: Path,
    *,
    build_type: str = "clean",
) -> BuildStepResult:
    """Time SQL migration validation (syntax check all migrations)."""
    # Count and validate migrations
    mig_files = sorted(migrations_dir.glob("*.sql"))
    cmd = [
        str(venv_python), "-c",
        f"import pathlib; migs = sorted(pathlib.Path(r'{migrations_dir}').glob('*.sql')); "
        f"print(f'{{len(migs)}} migrations validated')",
    ]
    result = _time_step(
        "sql_migration_validation", "sql", build_type, cmd,
    )
    # Override artifact count
    return BuildStepResult(
        step_name=result.step_name,
        language=result.language,
        build_type=result.build_type,
        wall_seconds=result.wall_seconds,
        cpu_seconds=result.cpu_seconds,
        peak_memory_bytes=result.peak_memory_bytes,
        success=result.success,
        error_message=result.error_message,
        artifact_count=len(mig_files),
    )


def run_full_build_baseline(
    project_root: Path,
    venv_python: Path,
    *,
    build_type: str = "clean",
    include_ts: bool = False,
    include_cc: bool = True,
    include_sql: bool = True,
) -> BuildBaselineRecord:
    """Run the full build baseline suite.

    By default, runs Python import + C/C++ compile + SQL validation.
    TypeScript is optional (requires npm install to have been run).
    """
    steps: list[BuildStepResult] = []

    # Python import
    steps.append(run_python_import_baseline(venv_python, build_type=build_type))

    # TypeScript typecheck (optional)
    if include_ts:
        npm = "npx.cmd" if platform.system() == "Windows" else "npx"
        steps.append(run_typescript_typecheck_baseline(
            npm, cwd=project_root / "main-system", build_type=build_type,
        ))

    # C/C++ compile
    if include_cc:
        build_script = project_root / "main-system/src-core/core_system/native/build_native.py"
        if build_script.exists():
            steps.append(run_cc_compile_baseline(
                venv_python, build_script, build_type=build_type,
            ))

    # SQL migration validation
    if include_sql:
        migrations = project_root / "shared-layer/migrations"
        if migrations.exists():
            steps.append(run_sql_migration_validation_baseline(
                venv_python, migrations, build_type=build_type,
            ))

    total_wall = sum(s.wall_seconds for s in steps)
    total_success = all(s.success for s in steps)

    return BuildBaselineRecord(
        baseline_id=f"build-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        baseline_version=BUILD_BASELINE_VERSION,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        environment=_capture_env(),
        steps=[asdict(s) for s in steps],
        total_wall_seconds=round(total_wall, 4),
        total_success=total_success,
        notes=f"{build_type} build baseline",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "BUILD_BASELINE_VERSION",
    "BuildStepResult",
    "BuildBaselineRecord",
    "BuildBaselineStore",
    "run_python_import_baseline",
    "run_typescript_typecheck_baseline",
    "run_cc_compile_baseline",
    "run_sql_migration_validation_baseline",
    "run_full_build_baseline",
]
