"""basis — 決策依據。

法典為唯一決策來源（P8/A12/E1）。所有主宰與星澄的決策只允許
引用法典條文代號（e.g. ``A26`` / ``E15``），禁止自然語言敘述
作為依據（A38/E24）。本模組僅以唯讀方式呈現法典條文，無執行權。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Iterable

from governance_rule.codex import (
    GOVERNANCE_CODEX,
    CodexArticle,
    CodexEdict,
)

PROVISION_PREFIXES: Final[tuple[str, ...]] = ("A", "E", "P")


def is_provision_token(value: str) -> bool:
    if not value:
        return False
    head = value[0]
    body = value[1:]
    return head in ("A", "E", "P") and body.isdigit()


@dataclass(frozen=True)
class DecisionBasis:
    """一組法典條文引用；建構即驗證代號型別（非自然語言）。"""

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


@dataclass(frozen=True)
class CodexView:
    """唯讀法典檢視：依據字面僅可供最少量呈現，不充當決策來源。"""

    codex_version: int
    principles: tuple[str, ...] = field(default_factory=tuple)
    articles: tuple[CodexArticle, ...] = field(default_factory=tuple)
    edicts: tuple[CodexEdict, ...] = field(default_factory=tuple)


def codex() -> CodexView:
    return CodexView(
        codex_version=GOVERNANCE_CODEX.codex_version,
        principles=tuple(principle.id for principle in GOVERNANCE_CODEX.principles),
        articles=tuple(GOVERNANCE_CODEX.articles),
        edicts=tuple(GOVERNANCE_CODEX.edicts),
    )


def provision_text(reference: str) -> str:
    if not is_provision_token(reference):
        raise ValueError(f"invalid provision token {reference!r}")
    kind, number = reference[0], reference[1:]
    if kind == "A":
        for article in GOVERNANCE_CODEX.articles:
            if article.id == reference:
                return article.rule
    if kind == "E":
        for edict in GOVERNANCE_CODEX.edicts:
            if edict.id == reference:
                return edict.edict
    if kind == "P":
        for principle in GOVERNANCE_CODEX.principles:
            if principle.id == reference:
                return principle.statement
    raise KeyError(f"unknown provision {reference!r}")


def verified_basis(references: Iterable[str]) -> DecisionBasis:
    """驗證每個引用確存在於法典，回傳凍結依據；不存在即丟錯（fail-closed）。"""
    tokens: list[str] = []
    for reference in references:
        if not is_provision_token(reference):
            raise ValueError(f"non-token basis: {reference!r}")
        provision_text(reference)
        tokens.append(reference)
    return DecisionBasis(tuple(tokens))


__all__ = [
    "DecisionBasis",
    "CodexView",
    "PROVISION_PREFIXES",
    "codex",
    "is_provision_token",
    "provision_text",
    "verified_basis",
]