from __future__ import annotations

from pathlib import Path
from typing import Any

def _contentful_indices(lines: list[str]) -> list[int]:
    indices: list[int] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            indices.append(index)
    return indices

def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]

def orphan_candidate_indices(lines: list[str]) -> list[int]:
    contentful = _contentful_indices(lines)
    candidates: list[int] = []
    position = 0
    while position < len(contentful):
        if _leading_whitespace(lines[contentful[position]]):
            position += 1
            continue
        start = position
        while position < len(contentful) and not _leading_whitespace(
            lines[contentful[position]]
        ):
            position += 1
        end = position - 1
        previous_indented = start > 0 and bool(
            _leading_whitespace(lines[contentful[start - 1]])
        )
        following_indented = (end + 1) < len(contentful) and bool(
            _leading_whitespace(lines[contentful[end + 1]])
        )
        if previous_indented and following_indented:
            candidates.extend(contentful[start : end + 1])
    return candidates

def candidate_indentations(lines: list[str], index: int) -> list[str]:
    contentful = _contentful_indices(lines)
    previous = ""
    following = ""
    observed: dict[str, int] = {}
    for sibling in contentful:
        indentation = _leading_whitespace(lines[sibling])
        if sibling < index:
            if indentation:
                previous = indentation
            if not indentation:
                observed.clear()
        elif sibling > index:
            if indentation:
                following = indentation
                break
    mode = max(observed, key=observed.get) if observed else ""
    candidates: list[str] = []
    for indentation in (previous, following, mode):
        if indentation and indentation not in candidates:
            candidates.append(indentation)
    return candidates

def _compiles(source: str) -> tuple[bool, str, str]:
    try:
        compile(source, "<source-repair>", "exec")
        return True, "", ""
    except (IndentationError, TabError, SyntaxError) as error:
        return False, error.__class__.__name__, str(error)

def _is_indentation_family(level: str, message: str) -> bool:
    if level in ("IndentationError", "TabError"):
        return True
    normalized = message.casefold()
    return any(
        signature.casefold() in normalized
        for signature in _SYNTAX_INDENTATION_SIGNATURES
    )

def syntax_problems(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return {
            "ok": False,
            "error": error.__class__.__name__,
            "message": str(error),
            "indentation_family": False,
        }
    ok, level, message = _compiles(source)
    return {
        "ok": ok,
        "error": level,
        "message": message,
        "indentation_family": _is_indentation_family(level, message),
    }

class IndentationRepairer:
    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines()

    def _single_solution(self) -> tuple[str, list[int]]:
        original_ok, _, _ = _compiles(self.source)
        if original_ok:
            raise ValueError("source already compiles")
        orphans = orphan_candidate_indices(self.lines)
        if not orphans or len(orphans) > MAX_ORPHANS_PER_FILE:
            raise ValueError("no bounded orphan candidate set")
        candidate_groups = [
            candidate_indentations(self.lines, index) for index in orphans
        ]
        if any(not group for group in candidate_groups):
            raise ValueError("orphan without candidate indentation")
        solutions: list[tuple[str, tuple[int, ...]]] = []
        patched = list(self.lines)
        applied: list[int] = []

        def search(position: int) -> None:
            if len(solutions) >= 2:
                return
            if position == len(orphans):
                text = "\n".join(patched)
                ok, _, _ = _compiles(text)
                if ok:
                    solutions.append((text, tuple(sorted(applied))))
                return
            index = orphans[position]
            for indentation in candidate_groups[position]:
                patched[index] = indentation + self.lines[index]
                applied.append(index)
                search(position + 1)
                applied.pop()
            patched[index] = self.lines[index]

        search(0)
        if len(solutions) != 1:
            raise ValueError("ambiguous or unreachable definite repair")
        return solutions[0]

    def repair(self) -> tuple[str, list[int]]:
        return self._single_solution()
