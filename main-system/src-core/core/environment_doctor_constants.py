"""Environment doctor — constants and helpers.

Provides the constants, requirement parsing, and helper functions
used by the environment doctor checks.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path



def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


REQUIRED_PROJECT_PATHS: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "src-core/main.py",
    "src-core/ipc/server.py",
    "src-ui/renderer",
)

REQUIRED_WORKSPACE_PATHS: tuple[str, ...] = (
    "governance_rule/governance_policy.py",
    "governance_rule/permission_directory/directory_authority.py",
)

# Runtime dependencies — the main-system venv MUST have these (pyproject
# ``dependencies``).  Everything else moved to optional extras when the
# dependency tree was slimmed; the doctor reports their presence without
# failing the environment on their absence.
REQUIRED_PYTHON_MODULES: dict[str, str] = {
    "psycopg": "psycopg",
    "websockets": "websockets",
}

# Optional extra groups (pyproject ``[project.optional-dependencies]``).
OPTIONAL_PYTHON_MODULE_GROUPS: dict[str, dict[str, str]] = {
    "test": {
        "pytest": "pytest",
        "pytest-asyncio": "pytest_asyncio",
    },
    "build": {
        "pybind11": "pybind11",
        "pyinstaller": "PyInstaller",
        "setuptools": "setuptools",
    },
    "local-model": {
        "imageio-ffmpeg": "imageio_ffmpeg",
        "numpy": "numpy",
        "pillow": "PIL",
        "sentence-transformers": "sentence_transformers",
        "torch": "torch",
    },
}

# External system-level tools the project depends on at runtime or build
# time.  These are NOT Python packages (those are in REQUIRED_PYTHON_MODULES)
# and NOT codex formal tools (those are declared in A49/E35).  Each entry
# maps the tool name to its command-line probe (``shutil.which``).
REQUIRED_EXTERNAL_TOOLS: dict[str, str] = {
    "git": "git",
    "node": "node",
    "npm": "npm.cmd",
    "python": "python",
}

# Optional external tools — the project degrades gracefully when these
# are unavailable, but they should be listed so the doctor can report
# their presence/absence.
OPTIONAL_EXTERNAL_TOOLS: dict[str, str] = {
    "ollama": "ollama",
    "playwright": "playwright",
    "cl": "cl",            # MSVC C++ compiler (build-time only)
    "nvidia-smi": "nvidia-smi",  # CUDA / GPU (optional acceleration)
    "ffmpeg": "ffmpeg",    # Standalone ffmpeg; imageio-ffmpeg bundles its own
}


def normalize_requirement_name(line: str) -> str | None:
    value = line.split("#", 1)[0].strip()
    if not value or value.startswith(("-r ", "--")):
        return None
    if " @ " in value:
        value = value.split(" @ ", 1)[0].strip()
    else:
        value = re.split(r"[<>=!~;\[\s]", value, maxsplit=1)[0].strip()
    return value.casefold().replace("_", "-") or None


def parse_requirement_names(requirements_path: Path) -> set[str]:
    try:
        lines = requirements_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {
        name
        for line in lines
        if (name := normalize_requirement_name(line)) is not None
    }


def default_python_executable(project_root: Path) -> Path:
    candidates = [
        project_root / ".venv" / "Scripts" / "python.exe",
        project_root / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


__all__ = [
    "_background_subprocess_kwargs",
    "REQUIRED_PROJECT_PATHS",
    "REQUIRED_WORKSPACE_PATHS",
    "REQUIRED_PYTHON_MODULES",
    "OPTIONAL_PYTHON_MODULE_GROUPS",
    "REQUIRED_EXTERNAL_TOOLS",
    "OPTIONAL_EXTERNAL_TOOLS",
    "normalize_requirement_name",
    "parse_requirement_names",
    "default_python_executable",
]
