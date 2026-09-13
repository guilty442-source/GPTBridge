from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_TEXT_SUFFIXES = {
    ".c", ".cpp", ".cs", ".css", ".go", ".html", ".java", ".js",
    ".json", ".md", ".py", ".rs", ".sql", ".ts", ".tsx", ".yaml", ".yml",
}
_SKIP_PARTS = {".git", ".venv", "dist", "node_modules", "runtime", "__pycache__"}


def collect_programming_context(folder: str, message: str) -> dict[str, Any]:
    """Run bounded read-only workspace tools for a coding request."""

    if not str(folder or "").strip():
        return {"ok": False, "error_code": "PROGRAMMING_FOLDER_REQUIRED"}
    root = Path(folder).resolve()
    if not root.is_dir():
        return {"ok": False, "error_code": "PROGRAMMING_FOLDER_NOT_FOUND"}

    files: list[Path] = []
    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part.casefold() in _SKIP_PARTS for part in relative.parts):
            continue
        if path.is_file() and path.suffix.casefold() in _TEXT_SUFFIXES:
            files.append(path)
            if len(files) >= 400:
                break

    terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_.-]{3,}", message)
    }
    ranked = sorted(
        files,
        key=lambda item: (
            not any(term in item.name.casefold() for term in terms),
            len(item.relative_to(root).parts),
            item.as_posix().casefold(),
        ),
    )
    excerpts: list[dict[str, str]] = []
    for path in ranked[:6]:
        try:
            content = path.read_text("utf-8", errors="replace")[:12_000]
        except OSError:
            continue
        excerpts.append(
            {"path": path.relative_to(root).as_posix(), "content": content}
        )

    return {
        "ok": True,
        "tools_called": ["workspace_list", "workspace_read"],
        "scope": str(root),
        "file_count_sampled": len(files),
        "files": [path.relative_to(root).as_posix() for path in ranked[:120]],
        "excerpts": excerpts,
        "read_only": True,
    }


def render_programming_context(result: dict[str, Any]) -> str:
    if result.get("ok") is not True:
        return ""
    lines = ["[受治理編程工具結果]", "檔案索引："]
    lines.extend(str(item) for item in result.get("files", []))
    for excerpt in result.get("excerpts", []):
        if not isinstance(excerpt, dict):
            continue
        lines.extend(
            (f"\n--- {excerpt.get('path', '')} ---", str(excerpt.get("content", "")))
        )
    return "\n".join(lines)


__all__ = ["collect_programming_context", "render_programming_context"]
