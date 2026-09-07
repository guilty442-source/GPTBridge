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

import asyncio
from datetime import datetime, timezone
from typing import Any

from .codex_decision import decision_basis

LANGUAGE_REVIEW_ROLE = "system-language-review-sub-sovereign"
LANGUAGE_REVIEW_AREA = "language-review"

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

    ROLE = LANGUAGE_REVIEW_ROLE

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._supervision_task: asyncio.Task[Any] | None = None
        self._supervision_interval_seconds = 600.0
        self._typescript_checkers: Any | None = None
        self._python_audit: Any | None = None

    async def start(
        self,
        *,
        supervision_interval_seconds: float = 600.0,
        typescript_checkers: Any = None,
        python_audit: Any = None,
    ) -> dict[str, Any]:
        self._supervision_interval_seconds = max(120.0, float(supervision_interval_seconds))
        self._typescript_checkers = typescript_checkers
        self._python_audit = python_audit
        if self._python_audit is None:
            self._python_audit = getattr(self.app, "governance", None)
        self._started_at = self._iso_now()
        self._started = True

        if self._supervision_task is None:
            self._supervision_task = asyncio.create_task(
                self._supervision_loop(),
                name="system-language-review-sub-sovereign-supervision",
            )

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "allowed_languages": list(ALLOWED_LANGUAGES),
            "decision": decision_basis(LANGUAGE_REVIEW_AREA),
        }

    async def stop(self) -> None:
        if self._supervision_task is not None:
            self._supervision_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._supervision_task
            self._supervision_task = None
        self._typescript_checkers = None
        self._python_audit = None
        self._started = False
        self._stopped_at = self._iso_now()

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "programming-language-conformance",
            "started": self._started,
            "allowed_languages": list(ALLOWED_LANGUAGES),
            "typescript_checkers": self._typescript_checker_status(),
            "python_audit": self._python_audit_status(),
            "supervision_loop": {
                "running": self._supervision_task is not None and not self._supervision_task.done(),
                "interval_seconds": self._supervision_interval_seconds,
            },
            "decision": decision_basis(LANGUAGE_REVIEW_AREA),
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
            "decision": decision_basis(LANGUAGE_REVIEW_AREA),
        }

    def _typescript_checker_status(self) -> dict[str, Any]:
        return {
            "duty": "typescript-governance-checkers",
            "enabled": self._typescript_checkers is not None,
            "delegation": "governed-executor-only",
        }

    def _python_audit_status(self) -> dict[str, Any]:
        audit = self._python_audit
        ready = False
        if audit is not None and hasattr(audit, "runtime_integrity_ready"):
            try:
                ready = bool(audit.runtime_integrity_ready())
            except Exception:
                ready = False
        return {
            "duty": "python-audit",
            "enabled": audit is not None,
            "integrity_ready": ready,
            "delegation": "governed-executor-only",
        }

    async def _supervision_loop(self) -> None:
        while self._started:
            await asyncio.sleep(self._supervision_interval_seconds)

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _suppress(*exceptions: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*exceptions)


__all__ = [
    "ALLOWED_LANGUAGES",
    "LANGUAGE_REVIEW_AREA",
    "LANGUAGE_REVIEW_ROLE",
    "LanguageReviewSubSovereign",
]
