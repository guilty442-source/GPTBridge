from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


DEFAULT_BLOCKED_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "dist-ui",
    "release",
    ".GPTBridge_CleanerQuarantine",
}


def _project_root() -> Path:
    return Path(os.environ.get("GPTBRIDGE_PROJECT_ROOT", Path.cwd())).resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_safe_path(
    path: str | os.PathLike[str],
    *,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
    blocked_names: Iterable[str] = DEFAULT_BLOCKED_NAMES,
    allow_missing: bool = True,
) -> bool:
    """Return whether a path is inside an allowed workspace boundary.

    The guard is intentionally conservative: empty paths, paths containing NUL
    bytes, protected dependency/build roots, and paths outside allowed roots are
    rejected. Missing paths can still be considered safe when their parent stays
    inside an allowed root, which is useful before creating a new file.
    """

    raw = os.fspath(path).strip()
    if not raw or "\x00" in raw:
        return False

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = _project_root() / candidate

    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return False

    roots = [Path(root).resolve(strict=False) for root in (allowed_roots or [_project_root()])]
    if not any(_is_relative_to(resolved, root) for root in roots):
        return False

    blocked = {name.casefold() for name in blocked_names}
    if any(part.casefold() in blocked for part in resolved.parts):
        return False

    if not allow_missing and not resolved.exists():
        return False

    return True
