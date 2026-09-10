"""Source size signal functions — A185/E160.

Information-layer signal payloads for source size violations.  These
functions produce signal dicts that must be routed through the information
layer; they never perform file mutation, split, or directory update.
"""

from __future__ import annotations

from typing import Any

from core_system.source_size_report import SizeReport


def size_violation_signal(
    report: SizeReport,
) -> dict[str, Any]:
    """Produce an information-layer signal for a size violation (A185/E160).

    Per A185: ``BLOCK:exceed-limit-before-merge+release+startup-activation-of-
    changed-module``.  This function produces the signal payload that must be
    routed through the information layer; it never performs file mutation,
    split, or directory update.
    """
    return {
        "signal_type": "source-size-violation",
        "authority": "signal-only",
        "basis": "A185/E160",
        "path": report.path,
        "ok": report.ok,
        "violations": [v.as_dict() for v in report.violations],
        "warnings": list(report.warnings),
        "action_required": "block-merge+block-release+block-startup-activation",
        "priority": "split-over-exception",
        "directory_entry": "permission-directory://source-structure",
    }
