"""Crash diagnosis and repair signaling for the boot core.

A72: ``BOOT-CORE:signal-and-request-only``.  These components diagnose
crash tracebacks and write repair signals to the information layer
(``repair-requests.json``) via ``RepairCoordinator``.  They never
execute repair mutations — that is the maintenance sovereign's
exclusive decision chain (A67/A72).

Extracted from ``central_repair`` to keep each module under the A73
600-effective-line boundary.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar


class CrashDiagnoser:
    """Dynamic crash diagnosis: turn recent child stdout into an action.

    This component never writes files.  It only decides whether the crash
    is a targetable source issue, a non-source error, or an unparseable
    traceback.  The repair decision is forwarded to ``CrashRepair``; the
    actual write is deferred to the maintenance sovereign's repair
    decision chain.
    """

    TAIL_LINES: ClassVar[int] = 50
    _RE_ERROR: ClassVar[re.Pattern[str]] = re.compile(
        r"^([A-Z]\w*(?:Error|Warning|Exception))\s*[:({]"
    )
    _RE_FRAME: ClassVar[re.Pattern[str]] = re.compile(
        r'^File\s+"([^"]+)",\s+line\s+(\d+),\s+in\s'
    )
    _INDENTATION_ERRORS: ClassVar[frozenset[str]] = frozenset(
        {"IndentationError", "TabError"}
    )
    # SyntaxError is only targetable when the message indicates an
    # indentation-family problem; a bare "invalid syntax" (e.g. missing
    # colon) is not fixable by the IndentationRepairer.
    _SYNTAX_INDENTATION_SIGNATURES: ClassVar[frozenset[str]] = frozenset(
        {
            "unexpected indent",
            "unindent does not match any outer indentation level",
            "expected an indented block",
            "inconsistent use of tabs and spaces",
        }
    )

    def diagnose(
        self,
        child_output: list[str],
        project_root: Path,
    ) -> dict[str, object]:
        """Parse the child output and return a diagnosis dict."""
        diagnosis: dict[str, object] = {
            "action": "fallback",
            "error_type": "",
            "file": "",
            "line": None,
        }
        if not child_output:
            return diagnosis

        error_type = ""
        error_file = ""
        error_line: int | None = None
        for raw in reversed(child_output[-self.TAIL_LINES :]):
            stripped = raw.strip()
            if not error_type:
                match = self._RE_ERROR.match(stripped)
                if match:
                    error_type = match.group(1)
            if not error_file:
                match = self._RE_FRAME.match(stripped)
                if match:
                    raw_file = match.group(1)
                    try:
                        error_line = int(match.group(2))
                        resolved = Path(raw_file).resolve()
                        error_file = resolved.relative_to(
                            project_root.resolve()
                        ).as_posix()
                    except (OSError, ValueError):
                        error_file = raw_file

        diagnosis["error_type"] = error_type
        diagnosis["file"] = error_file
        diagnosis["line"] = error_line

        if not error_file or not error_type:
            diagnosis["action"] = "fallback"
        elif error_type in self._INDENTATION_ERRORS:
            diagnosis["action"] = "targeted"
        elif error_type == "SyntaxError":
            # Only targetable if the message indicates an indentation
            # family issue; bare "invalid syntax" is not fixable by
            # the IndentationRepairer.
            tail = " ".join(
                line.strip()
                for line in child_output[-self.TAIL_LINES :]
            ).casefold()
            if any(sig in tail for sig in self._SYNTAX_INDENTATION_SIGNATURES):
                diagnosis["action"] = "targeted"
            else:
                diagnosis["action"] = "skip"
        else:
            diagnosis["action"] = "skip"
        return diagnosis


class CrashRepair:
    """Crash repair planner for the boot core.

    The boot core must not reset/overwrite source code on every startup
    failure.  This component only writes a repair signal to the
    information layer via ``RepairCoordinator``.  Actual execution is
    deferred to the maintenance sovereign's repair decision chain.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()

    def repair(self, diagnosis: dict[str, object]) -> dict[str, Any]:
        """Write a repair signal to the information layer (A67/A72).

        Per A72: ``BOOT-CORE:signal-and-request-only``.  The boot core
        must NOT execute repair mutations.  It only writes a signal to
        ``repair-requests.json`` via ``RepairCoordinator`` so the
        maintenance sovereign can pick it up, make the repair decision,
        and dispatch the governed executor.
        """
        report: dict[str, Any] = {
            "operation": "crash-repair-signal",
            "authority": "boot-core",
            "ok": False,
            "reason": "",
        }

        action = str(diagnosis.get("action", "fallback"))
        if action == "skip":
            report["reason"] = f"non-source: {diagnosis.get('error_type', 'unknown')}"
            return report
        if action != "targeted":
            report["reason"] = "unparseable-traceback; no targeted repair"
            return report

        # A72: write signal to the information layer via RepairCoordinator.
        # The boot core is a separate process from the backend, so it
        # instantiates its own RepairCoordinator pointing at the same
        # shared state file.
        try:
            from .repair_coordinator import RepairCoordinator

            coordinator = RepairCoordinator(self.project_root)
            target_file = str(diagnosis.get("file", ""))
            decision_proof = {
                "source": "boot-core-crash-diagnosis",
                "diagnosis": diagnosis,
                "exit_context": {
                    "error_type": str(diagnosis.get("error_type", "")),
                    "file": target_file,
                    "line": diagnosis.get("line"),
                },
            }
            signal_report = coordinator.request_governed_repair(
                failure_code="MAIN_SYSTEM_CRASH_REPAIR",
                owner="boot-core-crash-repair",
                decision_proof=decision_proof,
                signal_only=True,
            )
            report["ok"] = bool(signal_report.get("ok"))
            report["signal"] = signal_report
            report["targeted_file"] = target_file
            report["repair_plan"] = {
                "action": "targeted",
                "target_file": target_file,
            }
            report["reason"] = signal_report.get(
                "reason", "signal-written-to-information-layer"
            )
        except Exception as error:
            report["ok"] = False
            report["error"] = f"{type(error).__name__}: {error}"
        return report


__all__ = ["CrashDiagnoser", "CrashRepair"]
