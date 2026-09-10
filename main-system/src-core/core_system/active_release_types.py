"""Active release data structures — A181/E156 and A182/E157.

Per A181 (certified-hot-update-persistence-and-reset-prevention) and A182
(version-authority-and-repair-reset-prohibition), this module defines the
immutable data structures for the active release pointer, activation
ledger, and version mismatch signals.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Final

# A182: VERSION-NAMESPACES
VERSION_NAMESPACES: Final[tuple[str, ...]] = (
    "codex-version",
    "application-version",
    "tool-version",
    "release-id",
    "contract-version",
    "runtime-generation",
)

# A182: MISMATCH-CLASSIFICATION
MISMATCH_CLASSIFICATIONS: Final[tuple[str, ...]] = (
    "unknown",
    "metadata-drift",
    "artifact-drift",
    "contract-incompatible",
    "certificate-invalid",
    "release-pointer-invalid",
)


@dataclass(frozen=True)
class ActiveReleasePointer:
    """Content-addressed pointer to the current active certified release.

    Per A181: ``ACTIVE-RELEASE-IDENTITY:single-atomic-content-addressed-
    pointer to release-id+application-version+artifact-root+contract-root+
    certificate-digest+activation-generation``.
    """

    release_id: str
    application_version: str
    artifact_root: str
    contract_root: str
    certificate_digest: str
    activation_generation: str
    activated_at: str
    pointer_format: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def content_address(self) -> str:
        """Return a deterministic content-address hash for this pointer."""
        canonical = json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActivationLedgerEntry:
    """Append-only activation ledger record (A181: HISTORY)."""

    sequence: int
    operation: str
    from_release_id: str
    to_release_id: str
    reason: str
    decision_proof: str
    certificate_digest: str
    recorded_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VersionMismatch:
    """A typed version mismatch signal (A182: signal-not-mutation-authority).

    Per A182: ``AUTOMATIC-REPAIR+LEARNING+MODEL+AI-REASONING:may-read-typed-
    version-evidence+may-report-mismatch+may-propose-candidate but-may-not-
    infer/normalize/convert/increment/decrement/select/rewrite/publish/repair/
    version-or-release-identity``.
    """

    namespace: str
    classification: str
    expected: str
    actual: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_signal_only(self) -> bool:
        """Confirm this mismatch is a signal, not a mutation authority."""
        return True


__all__ = [
    "MISMATCH_CLASSIFICATIONS",
    "ActivationLedgerEntry",
    "ActiveReleasePointer",
    "VERSION_NAMESPACES",
    "VersionMismatch",
]
