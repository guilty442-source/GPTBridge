"""Official codex entry (A74/A173/A174) — the single read-only codex projection.

法典依據:
- A38/A107/A173: the official storage is the single local read-only SQLite
  database; the Chinese language mirror is backup/星澄-only and is never
  the adjudication, citation or machine-governance basis.
- A74: ALL-CODEX-CITATION enters through ``governance-codex://official``
  with an explicit provision id; resolution is read-only.
- A174: sovereign authoritative reads carry identity attestation, purpose
  and least provision scope; the Chinese view is 星澄-only — this module
  never reads the Chinese mirror.

Governed viewers (sub-sovereigns, adjudication layers) read declarations
through this module instead of importing the raw codex package, and every
attested read declares its requester scope and purpose.
"""

from __future__ import annotations

from typing import Any, Final

from governance_rule.execution.codex_repository import (
    CODEX_DATABASE_PATH,
    load_governance_codex,
)


OFFICIAL_ENTRY: Final[str] = "governance-codex://official"
_REQUEST_PURPOSES: Final[frozenset[str]] = frozenset(
    {"self-declaration", "adjudication", "status"}
)


def official_entry() -> str:
    """Return the canonical entry URI for codex viewers (A74)."""
    return OFFICIAL_ENTRY


def official_authority_path() -> str:
    """The official storage path — the single local SQLite (A107/A173)."""
    return str(CODEX_DATABASE_PATH)


def official_sovereign(
    sovereign_id: str,
    *,
    requester: str = "",
    purpose: str = "",
) -> Any | None:
    """Read one sovereign declaration from the official SQLite authority.

    Least provision scope (A174): an attested read may only resolve the
    requester's own declaration and must state a governed purpose; bare
    calls resolve a single import-time self-declaration.  The Chinese
    language mirror is never read here (A38/A173: it has no authority).
    """
    requested = str(sovereign_id or "").strip()
    if not requested:
        return None
    actor = str(requester or "").strip()
    if actor:
        if actor != requested:
            raise PermissionError("CODEX_SCOPE_DENIED")
        if str(purpose or "").strip() not in _REQUEST_PURPOSES:
            raise PermissionError("CODEX_PURPOSE_REQUIRED")
    codex = load_governance_codex()
    for item in codex.sovereigns:
        if item.id == requested:
            return item
    return None


__all__ = [
    "OFFICIAL_ENTRY",
    "official_authority_path",
    "official_entry",
    "official_sovereign",
]
