"""Bounded dead-code detection for tool-local cleanup (report-only).

Scans a tool root for Python modules that nothing inside the tool imports —
the same analysis used in the manual cleanup passes (e.g. the xingcheng
dead-code removal).  Findings are REPORT-ONLY: import analysis cannot see
dynamic imports, manifest/string references, or governance-audit-pinned
files, so candidates always require governed review before removal.  The
scan never deletes anything.

Wired into :func:`tool_local_cleanup.run_local_cleanup` so every daily
cleanup cycle also refreshes the tool's dead-code report, persisted to
``runtime/state/local-cleanup.json`` alongside the hygiene results.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

MAX_SCAN_FILES = 4000
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 1 * 1024 * 1024

# Directories never scanned for dead code.
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "dist-ui",
        "release",
        "runtime",
        "data",
        "electron-user-data",
        "browser-profile",
        "browser-profiles",
        "edge-profile",
        "backups",
        "__pycache__",
        ".worktrees",
        ".kilo",
    }
)

# Files that are entry points / pytest fixtures / package markers and are
# reachable without being imported.
_ENTRY_POINT_NAMES = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "conftest.py",
        "main.py",
        "cli.py",
        "channel_runtime.py",
        "setup.py",
    }
)

# Dynamic-import markers: when present anywhere in the tool's sources the
# whole report is marked low-confidence because a "zero-consumer" verdict
# may simply be an invisible dynamic edge.
_DYNAMIC_MARKERS = (
    "import_module",
    "importlib",
    "__import__",
    "exec(",
    "eval(",
)


def _iter_python_files(tool_root: Path) -> list[Path]:
    files: list[Path] = []
    for root, dir_names, file_names in os.walk(
        tool_root, topdown=True, followlinks=False
    ):
        dir_names[:] = [
            name for name in dir_names if name not in _EXCLUDED_DIRS
        ]
        for name in file_names:
            if not name.endswith(".py"):
                continue
            files.append(Path(root) / name)
            if len(files) >= MAX_SCAN_FILES:
                return files
    return files


def _package_parts(tool_root: Path, path: Path) -> tuple[str, ...]:
    """The file's package chain — parents that contain ``__init__.py``.

    ``src/``, ``backend/`` and similar layout directories may themselves be
    packages, so the chain can extend above the name importers actually use;
    suffix matching (see ``_suffixes``) absorbs that difference.
    """
    parts = [] if path.stem == "__init__" else [path.stem]
    parent = path.parent
    while (
        (parent / "__init__.py").is_file()
        and parent != tool_root.parent
        and len(parts) < 16
    ):
        parts.insert(0, parent.name)
        parent = parent.parent
    return tuple(parts)


def _suffixes(parts: tuple[str, ...]) -> set[str]:
    """Every dotted suffix of a module path — absorbs unknown sys.path roots."""
    return {
        ".".join(parts[index:])
        for index in range(len(parts))
        if parts[index]
    }


def _has_main_guard(tree: ast.AST) -> bool:
    """``if __name__ == "__main__":`` marks a self-executing entry point."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and any(
                isinstance(comparator, ast.Constant)
                and comparator.value == "__main__"
                for comparator in test.comparators
            )
        ):
            continue
        return True
    return False


def _imports_of(tree: ast.AST, package_parts: tuple[str, ...]) -> set[str]:
    """Imported module names (absolute + resolved-relative) for one file."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = ""
            if node.level:
                consumed = package_parts[: len(package_parts) - node.level + 1]
                base = ".".join(consumed)
            module = str(node.module or "")
            qualified = f"{base}.{module}".strip(".") if base else module
            if qualified:
                found.add(qualified)
            for alias in node.names:
                # ``from pkg import submodule`` — treat pkg.submodule as used.
                if qualified and alias.name != "*":
                    found.add(f"{qualified}.{alias.name}")
    return found


def _manifest_stems(tool_root: Path) -> set[str]:
    """Path stems referenced by manifests/configs — declared reachability."""
    stems: set[str] = set()
    manifests = [tool_root / "manifest.json"]
    try:
        manifests.extend(tool_root.glob("*.json"))
    except OSError:
        pass
    for manifest in manifests:
        try:
            if (
                not manifest.is_file()
                or manifest.stat().st_size > MAX_MANIFEST_BYTES
            ):
                continue
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, UnicodeError):
            payload = None
        blob = json.dumps(payload, ensure_ascii=False) if payload else text
        for token in blob.replace("\\", "/").split("/"):
            for piece in token.replace('"', " ").replace("'", " ").split():
                if piece.endswith(".py"):
                    stems.add(piece[:-3])
    return stems


def scan_dead_code(tool_root: Path | str) -> dict[str, Any]:
    """Report Python modules with no in-tool import consumer.

    Returns a report dict — never mutates the filesystem.  ``candidates``
    are review items for governed removal, not auto-delete targets.
    """
    root = Path(tool_root).resolve()
    if not root.is_dir():
        return {"ok": False, "error": "tool_root_missing", "candidates": []}

    files = _iter_python_files(root)
    manifest_stems = _manifest_stems(root)
    dotted: dict[Path, set[str]] = {}
    entry_points: set[Path] = set()
    imported_suffixes: set[str] = set()
    dynamic_present = False
    parse_failures: list[str] = []

    for path in files:
        relative = path.relative_to(root)
        package_parts = _package_parts(root, path)
        dotted[path] = _suffixes(package_parts) | {path.stem}
        try:
            size = path.stat().st_size
            if size > MAX_SOURCE_BYTES:
                parse_failures.append(str(relative))
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            parse_failures.append(str(relative))
            continue
        if _has_main_guard(tree):
            entry_points.add(path)
        # Relative imports resolve against the package chain, not the raw
        # directory layout (``..`` climbs packages only).  An ``__init__.py``
        # IS the package — its ``from . import x`` binds at its own level.
        owner_parts = (
            package_parts
            if path.stem == "__init__"
            else package_parts[:-1]
        )
        for name in _imports_of(tree, owner_parts):
            imported_suffixes |= _suffixes(tuple(name.split(".")))
        if not dynamic_present and any(
            marker in source for marker in _DYNAMIC_MARKERS
        ):
            dynamic_present = True

    candidates: list[dict[str, Any]] = []
    for path, names in dotted.items():
        relative = path.relative_to(root)
        if path.name in _ENTRY_POINT_NAMES or path in entry_points:
            continue
        if "tests" in relative.parts[:-1]:
            continue
        if path.stem in manifest_stems:
            continue
        if names & imported_suffixes:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        candidates.append(
            {
                "path": relative.as_posix(),
                "size_bytes": size,
                "consumer_kind": "none",
                "action": "report-only-governed-removal-required",
            }
        )

    return {
        "ok": True,
        "operation": "dead-code-scan",
        "authority": "tool-local-report-only",
        "tool_root": str(root),
        "scanned_files": len(files),
        "candidate_count": len(candidates),
        "candidates": candidates[:200],
        "dynamic_imports_present": dynamic_present,
        "parse_failures": parse_failures[:50],
        "exclusions": {
            "directories": sorted(_EXCLUDED_DIRS),
            "entry_point_names": sorted(_ENTRY_POINT_NAMES),
            "manifest_referenced_stems": len(manifest_stems),
        },
        "auto_delete": False,
        "governance_note": (
            "candidates are zero-import findings only; dynamic imports, "
            "audit-pinned adapters and string references are invisible to "
            "AST analysis — governed review required before removal"
        ),
    }


__all__ = ["scan_dead_code"]
