"""Codex amendment submitter — governed full-replacement through the information layer.

The submitter does not touch the authoritative codex file itself.  It packages a
proposed full replacement and dispatches it through the shared-layer information
channel, where the governed amendment executor applies it.

Governor ordinance amendment (request staged 2026-09-17): EVERY active
sovereign may originate a Codex amendment request.  A request is an initiation
only — it must follow the fixed A382/A488 non-disruptive amendment flow and it
may not release work division, publication or execution until all five active
sovereigns have audited the same immutable amendment_id and passed unanimously
(``CodexAmendmentAuditGate``).  An unknown requester or change class is denied
fail-closed (A10/A11, PERMISSION_DENIED).  No version arithmetic is performed:
a timestamp version identity is passed in by the amendment pipeline (A411),
while the legacy decimal form remains derivable for historical callers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

# The five active sovereigns (A377/A381 roster) may each originate an
# amendment request; sub-sovereigns and modules may not.
ACTIVE_SOVEREIGN_REQUESTERS: Final[tuple[str, ...]] = (
    "decision-sovereign",
    "permission-sovereign",
    "system-runtime-sovereign",
    "automation-sovereign",
    "xingcheng",
)

SOVEREIGN_ALIASES: Final[dict[str, str]] = {
    "星澄": "xingcheng",
    # A486: synchronization-sovereign canonically renamed automation-sovereign;
    # one-directional legacy-name resolution only.
    "synchronization-sovereign": "automation-sovereign",
}

# A412 change classes, ordered by impact; the pipeline records
# criterion/precedence/required-review alongside the class.
CHANGE_CLASSES: Final[tuple[str, ...]] = (
    "editorial",
    "clarification",
    "provision-scope",
    "authority-duty",
    "architecture-authority",
    "complete-reconstitution",
)

DEFAULT_CHANGE_CLASS: Final[str] = "provision-scope"

# A382/A488 fixed amendment flow and the mandatory gate that must pass before
# any division or execution (A537/A540).
AMENDMENT_FLOW: Final[str] = "A382/A488-non-disruptive-amendment-flow"
REQUIRED_GATE: Final[str] = "five-sovereign-audit-unanimous-pass"


class CodexAmendmentDenied(Exception):
    """Fail-closed denial of an unauthorized codex amendment request."""

    failure_code = "PERMISSION_DENIED"


@dataclass(frozen=True)
class CodexAmendmentRequest:
    """One sovereign-originated amendment request package (metadata only)."""

    amendment_id: str
    source_path: str
    previous_version: str
    new_version: str
    requested_by: str
    change_class: str
    flow: str = AMENDMENT_FLOW
    required_gate: str = REQUIRED_GATE
    sha256: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "command": "codex.amend",
            "amendment_id": self.amendment_id,
            "source_path": self.source_path,
            "previous_version": self.previous_version,
            "new_version": self.new_version,
            "requested_by": self.requested_by,
            "change_class": self.change_class,
            "flow": self.flow,
            "required_gate": self.required_gate,
            "sha256": self.sha256,
        }


def normalize_requester(requested_by: str) -> str:
    """Normalize and validate an amendment requester against the roster.

    Only the five active sovereigns may originate an amendment request; a
    sub-sovereign, module, tool or unknown actor is denied fail-closed.
    """
    value = SOVEREIGN_ALIASES.get(str(requested_by or "").strip(), str(requested_by or "").strip())
    if value not in ACTIVE_SOVEREIGN_REQUESTERS:
        raise CodexAmendmentDenied(
            f"codex amendment requester is not an active sovereign: {requested_by!r}"
        )
    return value


def normalize_change_class(change_class: str) -> str:
    value = str(change_class or "").strip().casefold().replace("_", "-")
    if value not in CHANGE_CLASSES:
        raise CodexAmendmentDenied(
            f"unknown codex amendment change class: {change_class!r}"
        )
    return value


class CodexAmendmentSubmitter:
    """Issue codex amendment requests through the information layer."""

    _VERSION_RE = re.compile(r"^\d+\.\d{5}$")

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()

    def next_version(self, current_version: str) -> str:
        """Auto-increment a 5-fractional-digit decimal version.

        Legacy compatibility only: current version identities are timestamp
        based (A411) and are supplied by the amendment pipeline instead of
        being derived here.
        """
        current = current_version.strip()
        if not self._VERSION_RE.match(current):
            raise ValueError(f"invalid codex version format: {current!r}")
        major, fractional = current.split(".")
        next_fractional = str(int(fractional) + 1).zfill(5)
        return f"{major}.{next_fractional}"

    def build_request(
        self,
        source_path: Path | str,
        current_version: str,
        requested_by: str,
        *,
        change_class: str = DEFAULT_CHANGE_CLASS,
        new_version: str | None = None,
    ) -> CodexAmendmentRequest:
        """Build and validate one sovereign amendment request.

        Raises ``CodexAmendmentDenied`` for an unauthorized requester or
        change class, ``FileNotFoundError`` when the staged replacement is
        absent and ``ValueError`` for an invalid replacement/version.
        """
        source = Path(source_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"codex amendment source not found: {source}")
        if source.suffix != ".sqlite3":
            raise ValueError("codex amendment source must be a .sqlite3 file")
        requester = normalize_requester(requested_by)
        normalized_class = normalize_change_class(change_class)
        previous = str(current_version or "").strip()
        if not previous:
            raise ValueError("codex amendment requires the current codex version")
        successor = str(new_version or "").strip() or self.next_version(previous)

        payload_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        amendment_id = f"codex-amend-{successor}-{payload_hash[:16]}"
        try:
            relative_source = source.relative_to(self.project_root).as_posix()
        except ValueError:
            relative_source = source.as_posix()
        return CodexAmendmentRequest(
            amendment_id=amendment_id,
            source_path=relative_source,
            previous_version=previous,
            new_version=successor,
            requested_by=requester,
            change_class=normalized_class,
            sha256=payload_hash,
        )

    def submit(
        self,
        channel: Any,
        source_path: Path | str,
        current_version: str,
        requested_by: str,
        *,
        change_class: str = DEFAULT_CHANGE_CLASS,
        new_version: str | None = None,
    ) -> str:
        """Submit a sovereign-originated full replacement to the information layer.

        ``channel`` must be an object with a ``request`` method matching
        ``shared_layer.channel.SharedLayerChannel``.  The dispatch carries the
        fixed-flow identity and the mandatory five-sovereign gate so the
        executor cannot lawfully skip either.
        """
        request = self.build_request(
            source_path,
            current_version,
            requested_by,
            change_class=change_class,
            new_version=new_version,
        )
        channel.request(
            "main-system",
            request.amendment_id,
            {
                **request.to_payload(),
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return request.amendment_id


__all__ = [
    "ACTIVE_SOVEREIGN_REQUESTERS",
    "AMENDMENT_FLOW",
    "CHANGE_CLASSES",
    "CodexAmendmentDenied",
    "CodexAmendmentRequest",
    "CodexAmendmentSubmitter",
    "DEFAULT_CHANGE_CLASS",
    "REQUIRED_GATE",
    "SOVEREIGN_ALIASES",
    "normalize_change_class",
    "normalize_requester",
]
