"""Version mismatch classification and signal — A182/E157.

Per A182 (version-authority-and-repair-reset-prohibition) and E157
(version-namespace-separation), version mismatches are signals, not
mutation authority.  This module classifies mismatches and produces
information-layer signal payloads.
"""

from __future__ import annotations

from typing import Any, Final

from core_system.active_release_types import (
    MISMATCH_CLASSIFICATIONS,
    VERSION_NAMESPACES,
    VersionMismatch,
)


def classify_version_mismatch(
    namespace: str,
    expected: str,
    actual: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> VersionMismatch:
    """Classify a version mismatch per A182/E157.

    Per A182: ``MISMATCH-CLASSIFICATION:unknown|metadata-drift|artifact-drift|
    contract-incompatible|certificate-invalid|release-pointer-invalid``.
    """
    if namespace not in VERSION_NAMESPACES:
        classification = "unknown"
    elif namespace in ("release-id", "runtime-generation"):
        classification = "release-pointer-invalid"
    elif namespace == "contract-version":
        classification = "contract-incompatible"
    elif namespace in ("codex-version", "application-version", "tool-version"):
        classification = "metadata-drift" if expected and actual and expected != actual else "unknown"
    else:
        classification = "unknown"
    return VersionMismatch(
        namespace=namespace,
        classification=classification,
        expected=expected,
        actual=actual,
        evidence=evidence or {},
    )


def version_mismatch_signal(
    mismatch: VersionMismatch,
) -> dict[str, Any]:
    """Produce an information-layer signal for a version mismatch (A182/E157).

    Per E157: ``VERSION-MISMATCH-RESET:none``.
    """
    return {
        "signal_type": "version-mismatch",
        "authority": "signal-only",
        "basis": "A182/E157",
        "namespace": mismatch.namespace,
        "classification": mismatch.classification,
        "expected": mismatch.expected,
        "actual": mismatch.actual,
        "evidence": mismatch.evidence,
        "action_required": "freeze-mutation+preserve-active-code+route-to-sovereign-decision",
        "repair_reset": False,
        "code_reset": False,
    }


__all__ = [
    "classify_version_mismatch",
    "version_mismatch_signal",
]
