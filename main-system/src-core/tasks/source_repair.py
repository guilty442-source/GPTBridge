from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

if __package__ in (None, ""):
    _SRC_CORE_ROOT = Path(__file__).resolve().parents[1]
    if str(_SRC_CORE_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_CORE_ROOT))

from core_system.versioning import component_version

from .source_repair_indent import (
    MAX_ORPHANS_PER_FILE,
    IndentationRepairer,
    candidate_indentations,
    orphan_candidate_indices,
    syntax_problems,
)

SOURCE_REPAIR_VERSION: Final[str] = component_version("source-repair")
SOURCE_REPAIR_RECIPE_ID: Final[str] = "main-system-python-source-syntax"
FAILURE_CODE: Final[str] = "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED"

# Backend source roots whose Python files are eligible for indentation
# self-repair.  Each entry is a repository-relative directory that contains
# authoritative backend Python source.  Front-end (src-ui), build output
# (dist, dist-ui), tests, scripts, and governance_rule/codex are intentionally
# excluded — they are either not Python, not backend, or read-only by codex.
# A184: independent tool source roots are excluded — main-system must not
# rewrite independent tool source.
SOURCE_ROOTS: Final[tuple[str, ...]] = (
    "main-system/src-core",
    "shared-layer/src",
)

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _git_has_uncommitted_change(project_root: Path, source_path: Path) -> bool:
    """Return True if ``source_path`` has uncommitted git modifications.

    The repair surface must never overwrite a file the developer is actively
    editing.  We ask git directly: if the path is unknown to the index, or
    the working-tree content differs from HEAD, the file is considered
    "dirty" and is skipped.  When git is installed but the check itself
    fails (error, timeout, non-zero exit) the file is treated as dirty so a
    repair never clobbers unverifiable in-progress edits.  Only when git is
    not installed at all is the file treated as "clean", so repair can still
    proceed in non-git environments.
    """
    try:
        relative = source_path.resolve(strict=False).relative_to(
            project_root.resolve()
        )
    except (OSError, ValueError):
        return False
    git_path = shutil.which("git")
    if not git_path:
        return False
    try:
        # `git status --porcelain` returns one line per dirty path.
        result = subprocess.run(
            [git_path, "status", "--porcelain", "--", relative.as_posix()],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


















class SourceRepairService:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def _recovery_root(self) -> Path:
        root = (
            self.project_root / "main-system" / "runtime" / "recovery" / "source"
        ).resolve()
        if not _inside(root, self.project_root):
            raise PermissionError("PERMISSION_DENIED")
        return root

    def python_sources(self) -> list[Path]:
        sources: list[Path] = []
        for relative in SOURCE_ROOTS:
            root = (self.project_root / relative).resolve()
            if not root.is_dir() or not _inside(root, self.project_root):
                continue
            for path in sorted(root.rglob("*.py")):
                if "_pycache_" in str(path):
                    continue
                if _inside(path, self.project_root):
                    sources.append(path)
        return sources

    def _hot_reload_protected(self, source_path: Path) -> bool:
        marker = (
            self.project_root / "main-system" / "runtime" / "state"
            / "hot-reload-protection.json"
        )
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            relative = source_path.resolve().relative_to(
                self.project_root
            ).as_posix()
            expected = str(
                (payload.get("protected_sources") or {}).get(relative) or ""
            )
            if not expected:
                return False
            actual = hashlib.sha256(source_path.read_bytes()).hexdigest()
            return actual == expected
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            return False

    def _backup(self, source_path: Path) -> None:
        if not _inside(source_path, self.project_root):
            raise PermissionError("PERMISSION_DENIED")
        relative = source_path.resolve(strict=False).relative_to(
            self.project_root.resolve()
        )
        backup_dir = self._recovery_root() / relative.parent
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(
            source_path, backup_dir / f"{source_path.name}.{stamp}.bak"
        )

    def _atomic_write(self, source_path: Path, content: str) -> None:
        temporary = source_path.with_name(f".{source_path.name}.repair.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, source_path)

    def _restore_latest(self, source_path: Path) -> None:
        if not _inside(source_path, self.project_root):
            return
        try:
            relative = source_path.resolve(strict=False).relative_to(
                self.project_root.resolve()
            )
        except (OSError, ValueError):
            return
        backup_dir = self._recovery_root() / relative.parent
        backups = sorted(backup_dir.glob(f"{source_path.name}.*.bak"))
        if not backups:
            return
        shutil.copy2(backups[-1], source_path)

    def self_repair(self) -> dict[str, Any]:
        # A181/E156: resolve active release pointer before repair.  The
        # active certified release is the only repair baseline; never reset
        # to packaged defaults, Git HEAD, startup snapshots, old caches,
        # installers, or prior releases.
        from core_system.active_release import resolve_active_pointer

        active_pointer = resolve_active_pointer()
        report: dict[str, Any] = {
            "operation": "automatic-source-repair",
            "authority": "main-system",
            "version": SOURCE_REPAIR_VERSION,
            "recipe_id": SOURCE_REPAIR_RECIPE_ID,
            "failure_code": FAILURE_CODE,
            "probed_sources": [],
            "problems": [],
            "repaired_files": [],
            "ambiguous_files": [],
            "skipped_dirty_files": [],
            "skipped_hot_reload_files": [],
            "errors": [],
            "active_release_id": active_pointer.release_id if active_pointer else "",
            "active_release_baseline": "A181/E156",
        }
        for source_path in self.python_sources():
            relative = source_path.relative_to(self.project_root).as_posix()
            report["probed_sources"].append(relative)
            self._repair_source_file(source_path, relative, report)
        report["ok"] = not report["errors"]
        return report

    def _repair_source_file(
        self,
        source_path: Path,
        relative: str,
        report: dict[str, Any],
    ) -> None:
        problem = syntax_problems(source_path)
        if problem.get("ok"):
            return
        report["problems"].append({"file": relative, **problem})
        if self._hot_reload_protected(source_path):
            report["skipped_hot_reload_files"].append(relative)
            return
        if not problem.get("indentation_family"):
            report["errors"].append(
                f"{relative}: {problem.get('error')}; not indentation-family"
            )
            return
        # Skip files with uncommitted developer changes to avoid
        # clobbering in-progress edits with an automated rewrite.
        if _git_has_uncommitted_change(self.project_root, source_path):
            report["skipped_dirty_files"].append(relative)
            return
        try:
            repairer = IndentationRepairer(
                source_path.read_text(encoding="utf-8")
            )
            repaired_source, repaired_indices = repairer.repair()
        except (OSError, UnicodeError, ValueError) as error:
            report["ambiguous_files"].append(
                {"file": relative, "reason": str(error)}
            )
            return
        # Re-check for uncommitted changes right before the write so an
        # edit saved between repair computation and write is preserved.
        if _git_has_uncommitted_change(self.project_root, source_path):
            report["skipped_dirty_files"].append(relative)
            return
        self._write_repaired_source(
            source_path, relative, repaired_source, repaired_indices, report
        )

    def _write_repaired_source(
        self,
        source_path: Path,
        relative: str,
        repaired_source: str,
        repaired_indices: list[int],
        report: dict[str, Any],
    ) -> None:
        try:
            self._backup(source_path)
            self._atomic_write(source_path, repaired_source)
        except (OSError, PermissionError) as error:
            report["errors"].append(
                f"{relative}: write failed ({error.__class__.__name__})"
            )
            return
        verification = syntax_problems(source_path)
        if not verification.get("ok"):
            report["errors"].append(
                f"{relative}: post-verification failed ({verification.get('error')})"
            )
            try:
                self._restore_latest(source_path)
            except (OSError, PermissionError):
                pass
            return
        report["repaired_files"].append(
            {
                "file": relative,
                "repaired_lines": repaired_indices,
                "verification": "compile-ok",
            }
        )


def self_repair_sources(project_root: Path, *, record: bool = True) -> dict[str, Any]:
    service = SourceRepairService(project_root)
    report = service.self_repair()
    # Ensure run_id is set for learning traceability.
    if not report.get("run_id"):
        report["run_id"] = uuid4().hex
    if not record:
        return report
    if not report.get("repaired_files") and not report.get("ambiguous_files"):
        return report
    from tasks.central_repair import RepairRunStore

    store = RepairRunStore(project_root / "main-system" / "data" / "automatic-repair")
    run: dict[str, Any] = {
        "run_id": report["run_id"],
        "target_tool_id": "main-system",
        "started_at": _iso_now(),
        "completed_at": _iso_now(),
        "failure_code": FAILURE_CODE,
        "ok": bool(report.get("repaired_files")),
        "operation": report.get("operation"),
        "authority": report.get("authority"),
        "version": report.get("version"),
        "recipe_id": report.get("recipe_id"),
        **report,
    }
    run["database"] = str(store.record("main-system", run))
    report["recorded_run"] = run["database"]
    # Code-repair learning: every probed code fault teaches the learner the
    # error class, the file it lives in, and the remedy that fixed it (or
    # failed to).  Repeated code faults then promote learned code recipes.
    _record_code_repair_learning(project_root, report)
    return report


def _record_code_repair_learning(
    project_root: Path,
    report: dict[str, Any],
) -> None:
    try:
        from tasks.repair_learning import record_code_repair
    except Exception:
        return
    problems = {
        str(entry.get("file") or ""): entry
        for entry in report.get("problems") or []
        if isinstance(entry, dict)
    }
    repaired = {
        str(entry.get("file") or "")
        for entry in report.get("repaired_files") or []
        if isinstance(entry, dict)
    }
    run_id = str(report.get("run_id") or "")
    for file_path, problem in problems.items():
        if not file_path:
            continue
        try:
            _record_single_code_repair(
                record_code_repair,
                project_root,
                file_path,
                problem,
                report,
                repaired,
                run_id,
            )
        except Exception:
            continue


def _record_single_code_repair(
    record_code_repair: Any,
    project_root: Path,
    file_path: str,
    problem: dict[str, Any],
    report: dict[str, Any],
    repaired: set[str],
    run_id: str,
) -> None:
    record_code_repair(
        project_root,
        file_path=file_path,
        error_class=str(problem.get("error") or "SyntaxError"),
        message=str(problem.get("message") or ""),
        remedy=(
            "column-0-indentation-recovery"
            if file_path in repaired
            else "source-repair-unresolved"
        ),
        ok=file_path in repaired,
        run_id=run_id,
        failure_code=str(report.get("failure_code") or FAILURE_CODE),
        extra_detail={
            "indentation_family": bool(problem.get("indentation_family")),
            "repaired_lines": next(
                (
                    entry.get("repaired_lines") or []
                    for entry in report.get("repaired_files") or []
                    if isinstance(entry, dict)
                    and str(entry.get("file") or "") == file_path
                ),
                [],
            ),
        },
    )


def _cli() -> int:
    parser = argparse.ArgumentParser(description="main-system source auto-repair agent")
    parser.add_argument("--self-repair", action="store_true")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--no-record", action="store_true")
    arguments = parser.parse_args()
    project_root = Path(
        arguments.project_root
        or os.environ.get("GPTBRIDGE_PROJECT_ROOT")
        or Path(__file__).resolve().parents[3]
    ).resolve()
    if not (project_root / "governance_rule").is_dir():
        print(json.dumps({"ok": False, "errors": ["invalid project root"]}))
        return 1
    if arguments.self_repair:
        report = self_repair_sources(project_root, record=not arguments.no_record)
        print(json.dumps(report, ensure_ascii=False))
        if report.get("errors"):
            return 3
        return 0 if report.get("ok") else 2
    print(json.dumps({"ok": True, "probe": "dry-run-only"}))
    return 0


__all__ = [
    "FAILURE_CODE",
    "IndentationRepairer",
    "SOURCE_REPAIR_RECIPE_ID",
    "SourceRepairService",
    "candidate_indentations",
    "orphan_candidate_indices",
    "self_repair_sources",
    "syntax_problems",
]

if __name__ == "__main__":
    raise SystemExit(_cli())
