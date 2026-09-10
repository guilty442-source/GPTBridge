"""Language Review Sub-Sovereign (system) — owns programming-language conformance.

Per the Governance Codex (A50 / P24, absorbed under the System Sovereign),
the Language Review Sub-Sovereign owns programming-language conformance,
acceptance, and migration review.  It ensures all system code adheres to the
four-language hybrid stack (Python + TypeScript + C++ + C) declared in A35/E21.

It is LOCAL CODE (same process as GPTBridgeApp) that coordinates existing
governance checkers (TypeScript checkers in governance_rule/execution/typescript/
and Python audit in governance_rule/execution/audit/) and DELEGATES the actual
enforcement to governed executors; it never holds an execution power itself.
"""

from __future__ import annotations

from typing import Any

from .codex_decision import decision_basis
from .sovereign_utils import _iso_now
from governance_rule.execution.tool_runtime.sub_sovereign import (
    SYSTEM_LANGUAGE_REVIEWER_AUTHORITY,
)
from governance_rule.codex import GOVERNANCE_CODEX


_LANGUAGE_REVIEW_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "code-language-review"),
    None,
)
if _LANGUAGE_REVIEW_SOVEREIGN is None:
    raise RuntimeError("language review sovereign not found in Governance Codex")

LANGUAGE_REVIEW_SUB_SOVEREIGN_RESPONSIBILITIES = _LANGUAGE_REVIEW_SOVEREIGN.duties

ALLOWED_LANGUAGES = ("python", "typescript", "cpp", "c", "csharp", "sql")


class LanguageReviewSubSovereign:
    """In-process sub-sovereign (under system) responsible for programming-language review.

    Responsibilities:
      - programming-language conformance (only python/typescript/cpp/c/csharp/sql)
      - language acceptance review for new modules
      - language migration review for legacy code
      - coordination of TypeScript governance checkers
      - coordination of Python audit checks
    """

    ROLE = _LANGUAGE_REVIEW_SOVEREIGN.id

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._typescript_checkers: Any | None = None
        self._python_audit: Any | None = None

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

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "programming-language-conformance",
            "started": self._started,
            "allowed_languages": list(ALLOWED_LANGUAGES),
            "typescript_checkers": self._typescript_checker_status(),
            "python_audit": self._python_audit_status(),
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
            "duty": "typescript-governance-checkers",
            "enabled": self._typescript_checkers is not None,
            "delegation": "governed-executor-only",
        }

    def _python_audit_status(self) -> dict[str, Any]:
        audit = self._python_audit
        return {
            "duty": "python-audit",
            "enabled": audit is not None,
            "health_owner": "maintenance-sovereign",
            "delegation": "governed-executor-only",
        }

__all__ = [
    "ALLOWED_LANGUAGES",
    "LANGUAGE_REVIEW_SUB_SOVEREIGN_RESPONSIBILITIES",
    "LanguageReviewSubSovereign",
]
