"""Language Review Sub-Sovereign — 程式語言審查子主權（子屬權限主宰，無決策、無執行）。

法典依據:
- sovereign_id: language-review-sub-sovereign (position 24)
- area: code-language-review
- rank: child-of-permission-sovereign-no-decision-no-execution
- basis: A308 (retires language-review-sovereign)

The implementation was merged from the retired
``core_system.language_review_sub_sovereign.LanguageReviewSubSovereign`` so
the active path keeps its programming-language conformance supervision
behavior while operating under the codex
``language-review-sub-sovereign`` identity (child of permission-sovereign).

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
governance checkers (TypeScript checkers in governance_rule/execution/typescript/
and Python audit in governance_rule/execution/audit/) and DELEGATES the actual
enforcement to governed executors; it never holds an execution power itself.
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase
from core_system.codex_decision import decision_basis
from core_system.sovereign_utils import _iso_now
from governance_rule.execution.tool_runtime.sub_sovereign import (
    SYSTEM_LANGUAGE_REVIEWER_AUTHORITY,
)
from governance_rule.codex import GOVERNANCE_CODEX


_LANGUAGE_REVIEW_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "code-language-review"),
    None,
)
if _LANGUAGE_REVIEW_SOVEREIGN is None:
    raise RuntimeError("language review sub-sovereign not found in Governance Codex")

ALLOWED_LANGUAGES = ("python", "typescript", "cpp", "c", "csharp", "sql")


class LanguageReviewSubSovereign(SubSovereignBase):
    """In-process sub-sovereign responsible for programming-language review.

    Responsibilities (codex sovereign definition — A308):
      - review-language-conformance (python/typescript/cpp/c/csharp/sql)
      - accept-language-migration (for new modules and legacy code)
      - review-generated-code-language (generated code conformance)
    """

    sovereign_id = "language-review-sub-sovereign"
    parent_sovereign_id = "permission-sovereign"

    ROLE = sovereign_id

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._typescript_checkers: Any | None = None
        self._python_audit: Any | None = None
        self._reviews: dict[str, dict[str, Any]] = {}

    async def start(
        self,
        *,
        typescript_checkers: Any = None,
        python_audit: Any = None,
    ) -> dict[str, Any]:
        self._typescript_checkers = typescript_checkers
        self._python_audit = python_audit
        if self._python_audit is None:
            self._python_audit = getattr(self.app, "governance", None)
        self._started_at = _iso_now()
        self._started = True

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "allowed_languages": list(ALLOWED_LANGUAGES),
            "decision": decision_basis(SYSTEM_LANGUAGE_REVIEWER_AUTHORITY),
        }

    async def stop(self) -> None:
        self._typescript_checkers = None
        self._python_audit = None
        self._started = False
        self._stopped_at = _iso_now()

    # ------------------------------------------------------------------
    # Review registry
    # ------------------------------------------------------------------

    def submit_review(self, review_id: str, payload: dict[str, Any]) -> None:
        """提交審查（由父層調用）。"""
        self._reviews[review_id] = {
            "payload": payload,
            "submitted_at": self._iso_now(),
            "status": "pending",
        }

    def complete_review(self, review_id: str, result: dict[str, Any]) -> None:
        """完成審查。"""
        if review_id in self._reviews:
            self._reviews[review_id].update({
                "result": result,
                "completed_at": self._iso_now(),
                "status": "completed",
            })

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "programming-language-conformance",
            "parent": self.parent_sovereign_id,
            "started": self._started,
            "allowed_languages": list(ALLOWED_LANGUAGES),
            "typescript_checkers": self._typescript_checker_status(),
            "python_audit": self._python_audit_status(),
            "reviews": {
                k: {"status": v["status"], "submitted_at": v["submitted_at"]}
                for k, v in self._reviews.items()
            },
            "decision": decision_basis(SYSTEM_LANGUAGE_REVIEWER_AUTHORITY),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "language-review",
            "role": self.ROLE,
            "scope": "programming-language-conformance-acceptance-migration",
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "allowed_languages": list(ALLOWED_LANGUAGES),
            "decision": decision_basis(SYSTEM_LANGUAGE_REVIEWER_AUTHORITY),
        }

    def _typescript_checker_status(self) -> dict[str, Any]:
        return {
            "duty": "review-language-conformance",
            "enabled": self._typescript_checkers is not None,
            "delegation": "governed-executor-only",
        }

    def _python_audit_status(self) -> dict[str, Any]:
        audit = self._python_audit
        return {
            "duty": "review-language-conformance",
            "enabled": audit is not None,
            "delegation": "governed-executor-only",
        }


__all__ = ["ALLOWED_LANGUAGES", "LanguageReviewSubSovereign"]
