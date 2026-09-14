"""Shared constants for xingcheng review mixins (A185 split).

Kept here to avoid circular imports between review.py and its mixin modules.
"""
from __future__ import annotations

# A137/A139: payload keys that would turn a review into a system effect
_REVIEW_FORBIDDEN_KEYS = frozenset({
    "execute", "write", "delete", "modify",
    "system_target", "operation", "target_path",
})

# A319: aspects examined for every permission request
_PERMISSION_REVIEW_ASPECTS = (
    "codex", "identity", "scope", "purpose",
    "least-privilege", "separation", "expiry", "risk", "current-evidence",
)

# A337: deterministic classification categories
_CLASSIFY_KINDS = (
    "intent", "task", "code", "fault", "evidence", "result",
)

# Language-review capability (transferred from abolished language-review-sub-sovereign)
ALLOWED_LANGUAGES = ("python", "typescript", "cpp", "c", "csharp", "sql")
_LANGUAGE_EXTENSIONS = {
    "python": {".py"},
    "typescript": {".ts", ".tsx"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hh"},
    "c": {".c", ".h"},
    "csharp": {".cs"},
    "sql": {".sql"},
}
_FILE_LINE_WARNING_THRESHOLD = 1000


__all__ = [
    "_REVIEW_FORBIDDEN_KEYS",
    "_PERMISSION_REVIEW_ASPECTS",
    "_CLASSIFY_KINDS",
    "ALLOWED_LANGUAGES",
    "_LANGUAGE_EXTENSIONS",
    "_FILE_LINE_WARNING_THRESHOLD",
]
