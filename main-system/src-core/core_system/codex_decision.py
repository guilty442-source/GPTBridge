"""Codex decision — shared decision basis for every sovereign.

Under the Codex, ALL decision authority lives in the Governance Codex (法典)
and is referenced — not owned — by each sovereign.  This module is the single
place that resolves a sovereign's decision from the Codex, so no sovereign
embeds its own decision source.

It is read-only and pure: it never mutates the codex and holds no callable
enforcement logic.  Each sovereign asks ``codex_edicts()`` for the edicts that
govern its area and uses them as its decision basis.
"""

from __future__ import annotations

from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX


def _by_area() -> dict[str, list[dict[str, str]]]:
    """Index Codex edicts by area (pure, read-only)."""

    indexed: dict[str, list[dict[str, str]]] = {}
    for edict in GOVERNANCE_CODEX.edicts:
        area = edict.area
        indexed.setdefault(area, []).append(
            {
                "id": edict.id,
                "edict": edict.edict,
                "immutability": edict.immutability,
            }
        )
    return indexed


def codex_edicts(area: str) -> list[dict[str, str]]:
    """Return the Codex edicts governing the given area (decision basis)."""

    return _by_area().get(area, [])


def _sovereign_for_area(area: str) -> dict[str, Any] | None:
    """Return the sovereign sub-law for the given area, if any."""

    for sovereign in GOVERNANCE_CODEX.sovereigns:
        if sovereign.area == area:
            return {
                "id": sovereign.id,
                "name": sovereign.name,
                "rank": sovereign.rank,
                "duties": list(sovereign.duties),
                "powers": list(sovereign.powers),
                "prohibitions": list(sovereign.prohibitions),
                "basis": sovereign.basis,
            }
    return None


def decision_basis(area: str) -> dict[str, Any]:
    """A sovereign's decision basis as referenced from the Codex.

    ``source`` always points at the Codex; an area with no dedicated edict
    still references the Codex's supreme authority preamble.  The matching
    sovereign sub-law is included when available.
    """

    edicts = codex_edicts(area)
    return {
        "decision_source": "governance-codex",
        "codex_schema": GOVERNANCE_CODEX.schema,
        "codex_version": GOVERNANCE_CODEX.codex_version,
        "authority_rank": GOVERNANCE_CODEX.preamble.authority_rank,
        "area": area,
        "edicts": edicts,
        "sovereign": _sovereign_for_area(area),
    }


__all__ = ["codex_edicts", "decision_basis"]
