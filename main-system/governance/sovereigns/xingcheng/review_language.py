"""Language review and codex read mixin for XingchengReviewMixin (A185 split).

Contains the language conformance review and the A144/A435 codex
read adjudication methods extracted from XingchengReviewMixin.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .._base import SovereignRequest, SovereignOutcome
from core_system.codex_decision import accepted_outcome, refusal_outcome

from .review_constants import (
    ALLOWED_LANGUAGES,
    _LANGUAGE_EXTENSIONS,
    _FILE_LINE_WARNING_THRESHOLD,
)


class XingchengLanguageReviewMixin:
    """Language review and codex read adjudication methods."""

    _reviews: dict[str, Any]

    def verified_basis(self, *codes: str) -> list[str]:
        raise NotImplementedError

    def _iso_now(self) -> str:
        raise NotImplementedError

    async def _adjudicate_language_review(self, request: SovereignRequest) -> SovereignOutcome:
        """Language conformance review (transferred capability, advisory only)."""
        language = str(request.payload.get("language") or "").lower()
        if language not in ALLOWED_LANGUAGES:
            return refusal_outcome("LANGUAGE_NOT_ALLOWED", self.verified_basis("A139"))
        files = request.payload.get("files") or []
        findings: list[dict[str, Any]] = []
        for entry in files:
            findings.extend(self._review_file(str(entry), language))
        review_id = f"language-review-{len(self._reviews) + 1}"
        self._reviews[review_id] = {
            "kind": "language-review",
            "language": language,
            "file_count": len(files),
            "findings": findings,
            "advisory": True,
            "reviewed_at": self._iso_now(),
        }
        return accepted_outcome(
            {
                "action": "language-review",
                "advisory": True,
                "review_id": review_id,
                "language": language,
                "file_count": len(files),
                "findings": findings,
            },
            self.verified_basis("A139", "A145"),
        )

    def _review_file(self, raw_path: str, language: str) -> list[dict[str, Any]]:
        """Read-only per-file conformance evidence."""
        findings: list[dict[str, Any]] = []
        path = Path(raw_path)
        if not path.exists():
            return [{
                "file": raw_path, "rule": "file-exists",
                "severity": "error", "detail": "file not found",
            }]
        expected = _LANGUAGE_EXTENSIONS[language]
        if path.suffix.lower() not in expected:
            findings.append({
                "file": raw_path,
                "rule": "language-extension",
                "severity": "warning",
                "detail": f"extension {path.suffix!r} not canonical for {language} ({sorted(expected)})",
            })
        try:
            line_count = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            line_count = -1
        if line_count > _FILE_LINE_WARNING_THRESHOLD:
            findings.append({
                "file": raw_path,
                "rule": "module-size",
                "severity": "warning",
                "detail": f"{line_count} lines exceeds {_FILE_LINE_WARNING_THRESHOLD}",
            })
        return findings

    async def _adjudicate_codex_read(self, request: SovereignRequest) -> SovereignOutcome:
        """A144/A435: 星澄-only official-entry codex read (review basis)."""
        scope = request.payload.get("scope") or "global-review"
        return accepted_outcome(
            {
                "codex_view": "official-entry",
                "mode": "read-only",
                "scope": scope,
                "session": "single-use",
                "audit": True,
                "permission_review": "exempt",
            },
            self.verified_basis("A435", "A144", "A145"),
        )


__all__ = ["XingchengLanguageReviewMixin"]
