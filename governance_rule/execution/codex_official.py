"""Official codex entry (A74/A132) — the single read-only codex projection.

法典依據:
- A74: ALL-CODEX-CITATION: enter-through-governance-codex://official
- A132: VIEW-ALLOWLIST:all-registered-sovereigns; ENTRY:governance-codex://official;
  MODE:read-only; PURPOSE:adjudication-only

Governed viewers (sub-sovereigns, adjudication layers) read the codex
through this module instead of importing the raw codex package directly.
The projection is read-only and pure: it loads the sealed codex repository
and exposes individual declarations without any mutation surface.
"""

from __future__ import annotations

from typing import Any, Final

from governance_rule.execution.codex_repository import load_governance_codex


OFFICIAL_ENTRY: Final[str] = "governance-codex://official"


def official_entry() -> str:
    """Return the canonical entry URI for codex viewers (A74)."""
    return OFFICIAL_ENTRY


def official_sovereign(sovereign_id: str) -> Any | None:
    """Read one sovereign declaration through the official entry (read-only)."""
    codex = load_governance_codex()
    for item in codex.sovereigns:
        if item.id == sovereign_id:
            return item
    return None


__all__ = ["OFFICIAL_ENTRY", "official_entry", "official_sovereign"]
