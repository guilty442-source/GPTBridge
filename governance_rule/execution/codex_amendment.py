"""Codex amendment submitter — governed full-replacement through the information layer.

The submitter does not touch the authoritative codex file itself.  It packages a
proposed full replacement and dispatches it through the shared-layer information
channel, where the System Sovereign's executor applies it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CodexAmendmentSubmitter:
    """Issue codex amendment requests through the information layer."""

    _VERSION_RE = re.compile(r"^\d+\.\d{5}$")

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()

    def next_version(self, current_version: str) -> str:
        """Auto-increment a 5-fractional-digit decimal version."""
        current = current_version.strip()
        if not self._VERSION_RE.match(current):
            raise ValueError(f"invalid codex version format: {current!r}")
        major, fractional = current.split(".")
        next_fractional = str(int(fractional) + 1).zfill(5)
        return f"{major}.{next_fractional}"

    def submit(
        self,
        channel: Any,
        source_path: Path | str,
        current_version: str,
        signed_by: str,
    ) -> str:
        """Submit a full replacement codex to the information layer.

        ``channel`` must be an object with a ``request`` method matching
        ``shared_layer.channel.SharedLayerChannel``.
        """
        source = Path(source_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"codex amendment source not found: {source}")
        if source.suffix != ".sqlite3":
            raise ValueError("codex amendment source must be a .sqlite3 file")

        new_version = self.next_version(current_version)
        payload_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        amendment_id = f"codex-amend-{new_version}-{payload_hash[:16]}"

        channel.request(
            "main-system",
            amendment_id,
            {
                "command": "codex.amend",
                "amendment_id": amendment_id,
                "source_path": str(source.relative_to(self.project_root)),
                "previous_version": current_version,
                "new_version": new_version,
                "signed_by": signed_by,
                "sha256": payload_hash,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return amendment_id


__all__ = ["CodexAmendmentSubmitter"]
