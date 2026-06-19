from __future__ import annotations

from pathlib import Path
from typing import Any


# --- Storage Paths ---
def ensure_backup_layout(project_root: Path) -> None:
    (project_root / "backups").mkdir(parents=True, exist_ok=True)
    (project_root / "backups" / "main-system").mkdir(parents=True, exist_ok=True)
    (project_root / "backups" / "design-mode").mkdir(parents=True, exist_ok=True)

def main_backup_root(project_root: Path) -> Path:
    return project_root / "backups" / "main-system"

def project_root_from(path: Path) -> Path:
    # This function would determine the project root from any given path within the project.
    # For simplicity, assuming the project root is two levels up from this file's location.
    return path.resolve().parent.parent

def backup_root(project_root: Path) -> Path:
    return project_root / "backups"

def design_backup_root(project_root: Path) -> Path:
    return project_root / "backups" / "design-mode"

def storage_layout(project_root: Path) -> dict[str, Any]:
    return {
        "project_root": str(project_root),
        "backup_root": str(backup_root(project_root)),
        "main_backup_root": str(main_backup_root(project_root)),
        "design_backup_root": str(design_backup_root(project_root)),
        "runtime_root": str(project_root / "runtime"),
        "sandbox_root": str(project_root / ".GPTBridge_RuntimeSandbox"),
    }

SANDBOX_ROOT_NAME = ".GPTBridge_RuntimeSandbox"

def sandbox_root(project_root: Path) -> Path:
    return project_root / SANDBOX_ROOT_NAME

def ensure_sandbox_layout(project_root: Path) -> None:
    s_root = sandbox_root(project_root)
    s_root.mkdir(parents=True, exist_ok=True)
    (s_root / "dev").mkdir(exist_ok=True)
    (s_root / "tool_build").mkdir(exist_ok=True)
    (s_root / "tool_test").mkdir(exist_ok=True)
    (s_root / "runtime").mkdir(exist_ok=True)
    (s_root / "logs").mkdir(exist_ok=True)
    (s_root / "cache").mkdir(exist_ok=True)
    (s_root / "artifacts").mkdir(exist_ok=True)
    (s_root / "temp").mkdir(exist_ok=True)


# --- Boundaries ---
def boundary_roots(project_root: Path, system_name: str) -> list[Path]:
    # This is a simplified version based on the ARCHITECTURE.md
    # In a real scenario, this would be more robust.
    if system_name == "core_system":
        return [
            project_root / "src-core",
            project_root / "src-ui", # Includes main and renderer
            project_root / "package.json",
            project_root / "vite.config.ts",
        ]
    return []