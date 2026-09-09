"""Read-only adapter for the authoritative Governance Codex SQLite file."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CodexPreamble:
    title: str
    authority_rank: str
    issuance: str
    binding_scope: str


@dataclass(frozen=True)
class CodexSection:
    index: str
    title: str
    summary: str


@dataclass(frozen=True)
class CodexPrinciple:
    id: str
    statement: str
    binding: bool


@dataclass(frozen=True)
class CodexArticle:
    id: str
    section: str
    subject: str
    rule: str
    prohibition: str = ""
    exception: str = ""


@dataclass(frozen=True)
class CodexEdict:
    id: str
    area: str
    edict: str
    immutability: str


@dataclass(frozen=True)
class CodexSavings:
    mutability: str
    function: str
    amendment: str
    overriding_authority: str
    interpretation: str
    conflict_resolution: str


@dataclass(frozen=True)
class CodexSovereign:
    id: str
    name: str
    area: str
    rank: str
    duties: tuple[str, ...]
    powers: tuple[str, ...]
    prohibitions: tuple[str, ...]
    basis: str


@dataclass(frozen=True)
class GovernanceCodex:
    schema: str
    codex_version: float
    preamble: CodexPreamble
    sections: tuple[CodexSection, ...] = field(default_factory=tuple)
    principles: tuple[CodexPrinciple, ...] = field(default_factory=tuple)
    articles: tuple[CodexArticle, ...] = field(default_factory=tuple)
    edicts: tuple[CodexEdict, ...] = field(default_factory=tuple)
    savings: CodexSavings | None = None
    sovereigns: tuple[CodexSovereign, ...] = field(default_factory=tuple)


CODEX_DATABASE_PATH = (
    Path(__file__).resolve().parents[1] / "codex" / "data" / "governance_codex.sqlite3"
)
CODEX_DATABASE = CODEX_DATABASE_PATH


def load_governance_codex(path: Path = CODEX_DATABASE_PATH) -> GovernanceCodex:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        preamble = connection.execute("SELECT * FROM preamble WHERE id=1").fetchone()
        savings = connection.execute("SELECT * FROM savings WHERE id=1").fetchone()
        lists = {
            table: {
                key: tuple(row[0] for row in connection.execute(
                    f"SELECT value FROM {table} WHERE sovereign_id=? ORDER BY position",
                    (key,),
                ))
                for key, in connection.execute("SELECT sovereign_id FROM sovereigns")
            }
            for table in ("sovereign_duties", "sovereign_powers", "sovereign_prohibitions")
        }
        return GovernanceCodex(
            schema=metadata["schema"],
            codex_version=float(metadata["codex_version"]),
            preamble=CodexPreamble(*(preamble[key] for key in ("title", "authority_rank", "issuance", "binding_scope"))),
            sections=tuple(CodexSection(row["section_index"], row["title"], row["summary"]) for row in connection.execute("SELECT * FROM sections ORDER BY position")),
            principles=tuple(CodexPrinciple(row["provision_id"], row["statement"], bool(row["binding"])) for row in connection.execute("SELECT * FROM principles ORDER BY position")),
            articles=tuple(CodexArticle(*(row[key] for key in ("provision_id", "section_index", "subject", "rule", "prohibition", "exception"))) for row in connection.execute("SELECT * FROM articles ORDER BY position")),
            edicts=tuple(CodexEdict(*(row[key] for key in ("provision_id", "area", "edict", "immutability"))) for row in connection.execute("SELECT * FROM edicts ORDER BY position")),
            savings=CodexSavings(*(savings[key] for key in ("mutability", "function", "amendment", "overriding_authority", "interpretation", "conflict_resolution"))),
            sovereigns=tuple(CodexSovereign(row["sovereign_id"], row["name"], row["area"], row["rank"], lists["sovereign_duties"][row["sovereign_id"]], lists["sovereign_powers"][row["sovereign_id"]], lists["sovereign_prohibitions"][row["sovereign_id"]], row["basis"]) for row in connection.execute("SELECT * FROM sovereigns ORDER BY position")),
        )
    finally:
        connection.close()


GOVERNANCE_CODEX = load_governance_codex()
