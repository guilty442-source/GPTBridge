"""Central repair — learning mixin.

Extracted from CentralRepairService: the learning helper methods
that record error signatures and repair outcomes for the
RepairLearner.
"""

from __future__ import annotations

import uuid
from typing import Any

from .repair_learning import (
    ErrorSignature,
    RepairOutcome,
    _normalize_error_signature,
)


class CentralRepairLearningMixin:
    """Learning helper methods for CentralRepairService."""

    def _learn_from_problem(self, problem: dict[str, Any], report: dict[str, Any]) -> None:
        """Record an error signature from a detected problem."""
        try:
            error_class = str(problem.get("error") or "Unknown")
            message = str(problem.get("message") or "")
            file_path = str(problem.get("file") or "")
            sig = ErrorSignature(
                signature_hash=_normalize_error_signature(
                    error_class, message, file_path=file_path
                ),
                error_class=error_class,
                message_pattern=message[:200],
                failure_code=str(report.get("failure_code") or "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED"),
                file_context=file_path,
                target_tool_id="main-system",
            )
            remedy = "indentation-repair" if problem.get("indentation_family") else "no-remedy"
            outcome = RepairOutcome(
                run_id=str(report.get("run_id") or uuid.uuid4().hex),
                signature_hash=sig.signature_hash,
                remedy=remedy,
                ok=remedy != "no-remedy",
                detail={"file": file_path, "error_class": error_class},
            )
            self.learner.learn_from_outcome(sig, outcome)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _learn_from_repair(self, repaired: dict[str, Any], report: dict[str, Any]) -> None:
        """Record a successful repair outcome for learning."""
        try:
            file_path = str(repaired.get("file") or "")
            error_class = "IndentationError"
            sig = ErrorSignature(
                signature_hash=_normalize_error_signature(error_class, "indentation", file_path=file_path),
                error_class=error_class,
                message_pattern="indentation",
                failure_code=str(report.get("failure_code") or "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED"),
                file_context=file_path,
                target_tool_id="main-system",
            )
            outcome = RepairOutcome(
                run_id=str(report.get("run_id") or uuid.uuid4().hex),
                signature_hash=sig.signature_hash,
                remedy="indentation-repair",
                ok=True,
                detail={"file": file_path, "repaired_lines": repaired.get("repaired_lines", [])},
            )
            self.learner.learn_from_outcome(sig, outcome)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _learn_from_tool_repair(
        self, target_id: str, failure_code: str, result: dict[str, Any]
    ) -> None:
        """Record a tool repair outcome for learning."""
        try:
            ok = bool(result.get("ok"))
            remedy = ",".join(result.get("executed_actions", []))
            sig = ErrorSignature(
                signature_hash=_normalize_error_signature(
                    failure_code, remedy, file_path=target_id
                ),
                error_class=failure_code,
                message_pattern=remedy[:200],
                failure_code=failure_code,
                file_context=target_id,
                target_tool_id=target_id,
            )
            outcome = RepairOutcome(
                run_id=str(result.get("run_id") or uuid.uuid4().hex),
                signature_hash=sig.signature_hash,
                remedy=remedy,
                ok=ok,
                detail={"target": target_id, "actions": result.get("executed_actions", [])},
            )
            self.learner.learn_from_outcome(sig, outcome)  # type: ignore[attr-defined]
        except Exception:
            pass
