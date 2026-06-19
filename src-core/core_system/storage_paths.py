from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


BACKUP_ROOT_NAME = "backups"
DESIGN_BACKUP_DIR_NAME = "design-mode"
MAIN_BACKUP_DIR_NAME = "main-system"
LEGACY_MAIN_BACKUP_DIR_NAMES = ("rescue-mode", "mother-audit")

SANDBOX_ROOT_NAME = ".GPTBridge_RuntimeSandbox"
SANDBOX_CHILD_DIRS = (
    "dev",
    "tool_build",
    "tool_test",
    "runtime",
    "logs",
    "cache",
    "artifacts",
    "temp",
)


def project_root_from(path: Path) -> Path:
    """Find the GPTBridge project root from a file or directory path."""
    project_root_override = os.environ.get("GPTBRIDGE_PROJECT_ROOT")
    if project_root_override:
        return Path(project_root_override).resolve()

    current = path.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "package.json").exists() and (candidate / "src-core").exists():
            return candidate
    raise RuntimeError(f"GPTBridge project root not found from {path}")


def backup_root(project_root: Path) -> Path:
    return project_root.resolve() / BACKUP_ROOT_NAME


def design_backup_root(project_root: Path) -> Path:
    return backup_root(project_root) / DESIGN_BACKUP_DIR_NAME


def main_backup_root(project_root: Path) -> Path:
    return backup_root(project_root) / MAIN_BACKUP_DIR_NAME


def legacy_main_backup_roots(project_root: Path) -> list[Path]:
    root = backup_root(project_root)
    return [root / name for name in LEGACY_MAIN_BACKUP_DIR_NAMES]


def sandbox_root(project_root: Path) -> Path:
    return project_root.resolve() / SANDBOX_ROOT_NAME


def ensure_backup_layout(project_root: Path) -> None:
    root = project_root.resolve()
    assert_backup_sandbox_separated(root)
    design_backup_root(root).mkdir(parents=True, exist_ok=True)
    main_backup_root(root).mkdir(parents=True, exist_ok=True)
    migrate_legacy_backups(root)


def ensure_sandbox_layout(project_root: Path) -> Path:
    root = project_root.resolve()
    assert_backup_sandbox_separated(root)
    sandbox = sandbox_root(root)
    sandbox.mkdir(parents=True, exist_ok=True)
    for name in SANDBOX_CHILD_DIRS:
        (sandbox / name).mkdir(parents=True, exist_ok=True)
    return sandbox


def assert_backup_sandbox_separated(project_root: Path) -> None:
    backups = backup_root(project_root).resolve()
    sandbox = sandbox_root(project_root).resolve()
    if backups == sandbox:
        raise RuntimeError("backup root cannot equal sandbox root")
    if backups in sandbox.parents:
        raise RuntimeError("sandbox root cannot be inside backup root")
    if sandbox in backups.parents:
        raise RuntimeError("backup root cannot be inside sandbox root")


def storage_layout(project_root: Path) -> dict[str, str | bool | list[str]]:
    root = project_root.resolve()
    return {
        "backup_root": str(backup_root(root)),
        "design_backup_root": str(design_backup_root(root)),
        "main_backup_root": str(main_backup_root(root)),
        "legacy_main_backup_roots": [str(path) for path in legacy_main_backup_roots(root)],
        "sandbox_root": str(sandbox_root(root)),
        "backup_sandbox_separated": True,
    }


def migrate_legacy_backups(project_root: Path) -> dict[str, Any]:
    """Move old scattered backup records into the governed backup folders."""
    root = project_root.resolve()
    backups = backup_root(root)
    main_root = main_backup_root(root)
    design_root = design_backup_root(root)
    backups.mkdir(parents=True, exist_ok=True)
    main_root.mkdir(parents=True, exist_ok=True)
    design_root.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    skipped: list[str] = []

    def unique_destination(target_dir: Path, name: str) -> Path:
        candidate = target_dir / name
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        for index in range(1, 1000):
            alternate = target_dir / f"{stem}-{index}{suffix}"
            if not alternate.exists():
                return alternate
        raise RuntimeError(f"cannot create unique backup destination for {candidate}")

    def move_item(source: Path, target_dir: Path) -> None:
        try:
            if not source.exists():
                return
            destination = unique_destination(target_dir, source.name)
            shutil.move(str(source), str(destination))
            moved.append(str(destination))
        except OSError as exc:
            skipped.append(f"{source}: {exc}")

    for source in backups.glob("*.zip"):
        move_item(source, main_root)

    for legacy_root in legacy_main_backup_roots(root):
        if not legacy_root.exists() or not legacy_root.is_dir():
            continue
        for source in sorted(legacy_root.iterdir(), key=lambda item: item.name):
            move_item(source, main_root)
        shutil.rmtree(legacy_root, ignore_errors=True)

    project_agent_root = backups / "project_agent"
    if project_agent_root.exists() and project_agent_root.is_dir():
        target_root = main_root / "project-agent"
        target_root.mkdir(parents=True, exist_ok=True)
        for source in sorted(project_agent_root.iterdir(), key=lambda item: item.name):
            move_item(source, target_root)
        shutil.rmtree(project_agent_root, ignore_errors=True)

    return {
        "ok": not skipped,
        "moved": moved,
        "skipped": skipped,
        "design_backup_root": str(design_root),
        "main_backup_root": str(main_root),
    }
