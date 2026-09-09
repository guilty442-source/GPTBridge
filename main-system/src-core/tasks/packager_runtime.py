from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from packager_base import (
    ELECTRON_DIST_DIR,
    _background_subprocess_kwargs,
)


SENSITIVE_RUNTIME_DIRECTORY_NAMES = frozenset(
    {
        "browser-profile",
        "browser-profiles",
        "edge-profile",
        "runtime",
        "log",
        "logs",
        "import",
        "imports",
        "export",
        "exports",
    }
)
SENSITIVE_RUNTIME_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".ivault",
    ".jsonl",
    ".log",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
PYTHON_RUNTIME_EXCLUDED_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "_pytest",
        "_pyinstaller_hooks_contrib",
        "ensurepip",
        "idlelib",
        "networkx",
        "pip",
        "PyInstaller",
        "pygments",
        "pytest",
        "scipy",
        "sympy",
        "tests",
        "tkinter",
        "tokenizers",
        "torch",
        "torchaudio",
        "torchvision",
        "transformers",
    }
)
PYTHON_RUNTIME_EXCLUDED_PREFIXES = (
    "networkx-",
    "pip-",
    "pyinstaller-",
    "pyinstaller_hooks_contrib-",
    "pygments-",
    "pytest-",
    "pytest_asyncio-",
    "scipy-",
    "sympy-",
    "tokenizers-",
    "torch-",
    "torchaudio-",
    "torchvision-",
    "transformers-",
)

REQUIRED_RUNTIME_IMPORTS = ("websockets",)
TOOL_REQUIRED_RUNTIME_IMPORTS: dict[str, tuple[str, ...]] = {
    "file-sorter": ("PIL", "imageio_ffmpeg"),
    "vaultly": ("imageio_ffmpeg",),
}


def package_copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lowered = name.lower()
        if lowered in SENSITIVE_RUNTIME_DIRECTORY_NAMES:
            ignored.add(name)
            continue
        if lowered in {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        }:
            ignored.add(name)
            continue
        if lowered.endswith((".pyc", ".pyo")):
            ignored.add(name)
            continue
        if any(lowered.endswith(suffix) for suffix in SENSITIVE_RUNTIME_SUFFIXES):
            ignored.add(name)
    return ignored


def python_runtime_copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lowered = name.lower()
        if lowered in {item.lower() for item in PYTHON_RUNTIME_EXCLUDED_NAMES}:
            ignored.add(name)
            continue
        if any(
            lowered.startswith(prefix.lower())
            for prefix in PYTHON_RUNTIME_EXCLUDED_PREFIXES
        ):
            ignored.add(name)
            continue
        if lowered.endswith((".pyc", ".pyo", ".pth")):
            ignored.add(name)
    return ignored


def copy_portable_python_runtime(
    app_dir: Path,
    *,
    base_prefix: Path | None = None,
    environment_prefix: Path | None = None,
) -> Path:
    base = (base_prefix or Path(sys.base_prefix)).resolve()
    environment = (environment_prefix or Path(sys.prefix)).resolve()
    target = app_dir / "python"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    root_runtime_files = [
        candidate
        for candidate in base.iterdir()
        if candidate.is_file()
        and (
            candidate.suffix.lower() in {".dll", ".exe"}
            or candidate.name.lower() in {"license.txt", "news.txt"}
        )
    ]
    if not any(candidate.name.lower() == "python.exe" for candidate in root_runtime_files):
        raise RuntimeError(f"Portable Python executable not found under: {base}")
    for source in root_runtime_files:
        shutil.copy2(source, target / source.name)

    for directory_name in ("DLLs", "Lib"):
        source = base / directory_name
        if not source.is_dir():
            raise RuntimeError(f"Portable Python directory not found: {source}")
        shutil.copytree(
            source,
            target / directory_name,
            ignore=python_runtime_copy_ignore,
        )

    source_site_packages = environment / "Lib" / "site-packages"
    target_site_packages = target / "Lib" / "site-packages"
    if source_site_packages.is_dir() and source_site_packages != (
        base / "Lib" / "site-packages"
    ):
        if target_site_packages.exists():
            shutil.rmtree(target_site_packages)
        shutil.copytree(
            source_site_packages,
            target_site_packages,
            ignore=python_runtime_copy_ignore,
        )
    if not (target / "python.exe").exists():
        raise RuntimeError("Portable Python runtime copy verification failed")
    return target


def validate_staged_python_runtime(
    app_dir: Path,
    tool_id: str,
    backend_entry_relative: str,
) -> None:
    """Fail before promotion when an isolated runtime lost a required feature."""

    runtime = app_dir / "python" / "python.exe"
    backend_entry = app_dir / Path(backend_entry_relative)
    imports = tuple(
        dict.fromkeys(
            (
                *REQUIRED_RUNTIME_IMPORTS,
                *TOOL_REQUIRED_RUNTIME_IMPORTS.get(tool_id, ()),
            )
        )
    )
    import_probe = (
        "import importlib,json,sys;"
        "[importlib.import_module(name) for name in json.loads(sys.argv[1])]"
    )
    probes = (
        (
            [
                str(runtime),
                "-B",
                "-s",
                "-E",
                "-X",
                "utf8",
                "-c",
                import_probe,
                json.dumps(imports),
            ],
            "required dependency import",
        ),
        (
            [
                str(runtime),
                "-B",
                "-s",
                "-E",
                "-X",
                "utf8",
                "-c",
                (
                    "import pathlib,sys;"
                    "source=pathlib.Path(sys.argv[1]).read_text(encoding='utf-8');"
                    "compile(source,sys.argv[1],'exec')"
                ),
                str(backend_entry),
            ],
            "standalone backend startup import",
        ),
    )
    for command, label in probes:
        completed = subprocess.run(
            command,
            cwd=app_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
            **_background_subprocess_kwargs(),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"Staged Python {label} failed for {tool_id}: "
                f"{detail or f'exit code {completed.returncode}'}"
            )


def copy_file_preserving_locked_target(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if source.samefile(target):
            return
        target.unlink()
    try:
        os.link(source, target)
        return
    except OSError:
        pass

    shutil.copy2(source, target)


def copy_runtime_item(source: Path, target: Path) -> None:
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_runtime_item(child, target / child.name)
        return
    copy_file_preserving_locked_target(source, target)


def copy_electron_runtime(dist_dir: Path, exe_path: Path) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)

    for source in ELECTRON_DIST_DIR.iterdir():
        target = dist_dir / source.name
        if source.name == "electron.exe":
            continue
        copy_runtime_item(source, target)

    copy_file_preserving_locked_target(ELECTRON_DIST_DIR / "electron.exe", exe_path)
    leftover_electron = dist_dir / "electron.exe"
    if leftover_electron.exists():
        leftover_electron.unlink()
