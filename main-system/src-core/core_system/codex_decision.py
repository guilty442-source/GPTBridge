"""Codex decision — shared decision basis for every sovereign.

Under the Codex, ALL decision authority lives in the Governance Codex (法典)
and is referenced — not owned — by each sovereign.  This module is the single
place that resolves a sovereign's decision from the Codex, so no sovereign
embeds its own decision source.

It is read-only and pure: it never mutates the codex and holds no callable
enforcement logic.  Each sovereign asks ``codex_edicts()`` for the edicts that
govern its area and uses them as its decision basis.

This module also provides ``DecisionBasis`` and ``verified_basis()`` —
formal provision-token verification (A38/E24: no natural-language basis).
These patterns are extracted from ``arch-next/codex_arch/shared/basis.py``
and integrated here as the production decision-layer foundation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from governance_rule.execution.codex_repository import (
    format_codex_version,
    load_governance_codex,
)


# ---------------------------------------------------------------------------
# Provision token verification (from arch-next/codex_arch/shared/basis.py)
# ---------------------------------------------------------------------------

def is_provision_token(value: str) -> bool:
    """Check if ``value`` is a codex provision token (A/E/P + digits)."""
    if not value:
        return False
    head = value[0]
    body = value[1:]
    return head in ("A", "E", "P") and body.isdigit()


def provision_text(reference: str) -> str:
    """Return the rule/edict/statement text for a provision token.

    Raises KeyError if the token is valid but not found in the codex.
    Raises ValueError if the token format is invalid.
    """
    if not is_provision_token(reference):
        raise ValueError(f"invalid provision token {reference!r}")
    kind, _number = reference[0], reference[1:]
    if kind == "A":
        for article in load_governance_codex().articles:
            if article.id == reference:
                return article.rule
    if kind == "E":
        for edict in load_governance_codex().edicts:
            if edict.id == reference:
                return edict.edict
    if kind == "P":
        for principle in load_governance_codex().principles:
            if principle.id == reference:
                return principle.statement
    raise KeyError(f"unknown provision {reference!r}")


@dataclass(frozen=True)
class DecisionBasis:
    """A set of codex provision references; validated at construction.

    Per A38/E24, decision basis must be codex provision tokens (e.g.
    ``A26`` / ``E15``), not natural-language descriptions.
    """

    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for reference in self.references:
            if not is_provision_token(reference):
                raise ValueError(
                    f"decision basis must be codex provision tokens, got {reference!r}"
                )

    @property
    def provisions(self) -> tuple[str, ...]:
        return self.references


def verified_basis(references: Iterable[str]) -> DecisionBasis:
    """Verify each reference exists in the codex; return frozen basis.

    Fail-closed: raises if any reference is not a valid token or not found.
    """
    tokens: list[str] = []
    for reference in references:
        if not is_provision_token(reference):
            raise ValueError(f"non-token basis: {reference!r}")
        provision_text(reference)  # raises KeyError if not found
        tokens.append(reference)
    return DecisionBasis(tuple(tokens))


# ---------------------------------------------------------------------------
# Sovereign-layer contracts (from arch-next/codex_arch/shared/contracts.py)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SovereignRequest:
    """A request to a sovereign's single entry gate.

    intent and subject are codex-style tokens (kebab-case), not natural
    language; requester must be an issued role (A7/A10).
    """

    intent: str
    subject: str
    requester: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Refusal:
    """Fail-closed refusal record (A11): does not expose internal permission details."""

    reason_code: str
    basis: tuple[str, ...] = ()


@dataclass(frozen=True)
class SovereignOutcome:
    """Unified output of a sovereign decision.

    accepted is always False by default; only explicit allow-listing
    sets it to True (A10).
    """

    accepted: bool = False
    refusal: Refusal | None = None
    result: dict[str, Any] = field(default_factory=dict)
    basis: tuple[str, ...] = ()


def refusal_outcome(reason_code: str, basis: tuple[str, ...]) -> SovereignOutcome:
    return SovereignOutcome(accepted=False, refusal=Refusal(reason_code, basis), basis=basis)


def accepted_outcome(result: dict[str, Any], basis: tuple[str, ...]) -> SovereignOutcome:
    return SovereignOutcome(accepted=True, result=result, basis=basis)


# ---------------------------------------------------------------------------
# Codex edict indexing (existing production interface)
# ---------------------------------------------------------------------------

def _by_area() -> dict[str, list[dict[str, str]]]:
    """Index Codex edicts by area (pure, read-only)."""

    indexed: dict[str, list[dict[str, str]]] = {}
    for edict in load_governance_codex().edicts:
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

    for sovereign in load_governance_codex().sovereigns:
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
    codex_snapshot = load_governance_codex()
    return {
        "decision_source": "governance-codex",
        "codex_schema": codex_snapshot.schema,
        "codex_version": format_codex_version(codex_snapshot.codex_version),
        "authority_rank": codex_snapshot.preamble.authority_rank,
        "area": area,
        "edicts": edicts,
        "sovereign": _sovereign_for_area(area),
    }


__all__ = [
    "DecisionBasis",
    "Refusal",
    "SovereignOutcome",
    "SovereignRequest",
    "accepted_outcome",
    "codex_edicts",
    "decision_basis",
    "is_provision_token",
    "provision_text",
    "refusal_outcome",
    "verified_basis",
]
