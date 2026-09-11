"""Read-only adapter for the authoritative Governance Codex SQLite file."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


CODEX_VERSION_UNIT: Final[int] = 100_000


def codex_version_units(value: object) -> int:
    """Convert ``integer-dot-five-decimal-digits`` version text to integer units.

    A100 mandates integer-only version arithmetic and forbids floating point.
    Versions are therefore represented as whole counts of the ``0.00001`` unit
    (e.g. ``1.41600`` -> ``141600``) rather than floats.
    """
    text = str(value or "").strip()
    whole, separator, fraction = text.partition(".")
    if (
        not separator
        or not whole.isdigit()
        or not fraction.isdigit()
        or len(fraction) != 5
    ):
        raise ValueError(
            f"codex version must be integer-dot-five-decimal-digits: {value!r}"
        )
    return int(whole) * CODEX_VERSION_UNIT + int(fraction)


def format_codex_version(version: int) -> str:
    """Render an integer micro-unit version as ``integer-dot-five-decimal-digits``."""
    return f"{version // CODEX_VERSION_UNIT}.{version % CODEX_VERSION_UNIT:05d}"


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
class CodexDirectoryRow:
    """One immutable row of an authoritative codex directory (A223-A227/A228)."""

    fields: tuple[tuple[str, str], ...]

    def get(self, name: str) -> str | None:
        for key, value in self.fields:
            if key == name:
                return value
        return None

    def as_dict(self) -> dict[str, str]:
        return dict(self.fields)


@dataclass(frozen=True)
class GovernanceCodex:
    schema: str
    codex_version: int
    preamble: CodexPreamble
    sections: tuple[CodexSection, ...] = field(default_factory=tuple)
    principles: tuple[CodexPrinciple, ...] = field(default_factory=tuple)
    articles: tuple[CodexArticle, ...] = field(default_factory=tuple)
    edicts: tuple[CodexEdict, ...] = field(default_factory=tuple)
    savings: CodexSavings | None = None
    sovereigns: tuple[CodexSovereign, ...] = field(default_factory=tuple)
    directories: dict[str, tuple[CodexDirectoryRow, ...]] = field(default_factory=dict)


CODEX_DATABASE_PATH = (
    Path(__file__).resolve().parents[1] / "codex" / "data" / "governance_codex.sqlite3"
)
CODEX_DATABASE = CODEX_DATABASE_PATH


def _load_codex_directories(
    connection: sqlite3.Connection,
) -> dict[str, tuple[CodexDirectoryRow, ...]]:
    """Read every authoritative ``*_directory`` table (A223-A227/A228) read-only.

    Directories are permission-sovereign-owned identity registries inside the
    codex; this session only makes their registered content available to
    governed readers, never writes.
    """
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name LIKE '%_directory' ORDER BY name"
        )
    ]
    directories: dict[str, tuple[CodexDirectoryRow, ...]] = {}
    for table in tables:
        column_names = [
            column[1]
            for column in connection.execute(f"PRAGMA table_info({table})")
        ]
        rows: list[CodexDirectoryRow] = []
        for row in connection.execute(
            f"SELECT {', '.join(column_names)} FROM {table} ORDER BY 1"
        ):
            fields = tuple(
                (name, str(value)) for name, value in zip(column_names, row)
            )
            rows.append(CodexDirectoryRow(fields))
        directories[table] = tuple(rows)
    return directories


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
            codex_version=codex_version_units(metadata["codex_version"]),
            preamble=CodexPreamble(*(preamble[key] for key in ("title", "authority_rank", "issuance", "binding_scope"))),
            sections=tuple(CodexSection(row["section_index"], row["title"], row["summary"]) for row in connection.execute("SELECT * FROM sections ORDER BY position")),
            principles=tuple(CodexPrinciple(row["provision_id"], row["statement"], bool(row["binding"])) for row in connection.execute("SELECT * FROM principles ORDER BY position")),
            articles=tuple(CodexArticle(*(row[key] for key in ("provision_id", "section_index", "subject", "rule", "prohibition", "exception"))) for row in connection.execute("SELECT * FROM articles ORDER BY position")),
            edicts=tuple(CodexEdict(*(row[key] for key in ("provision_id", "area", "edict", "immutability"))) for row in connection.execute("SELECT * FROM edicts ORDER BY position")),
            savings=CodexSavings(*(savings[key] for key in ("mutability", "function", "amendment", "overriding_authority", "interpretation", "conflict_resolution"))),
            sovereigns=tuple(CodexSovereign(row["sovereign_id"], row["name"], row["area"], row["rank"], lists["sovereign_duties"][row["sovereign_id"]], lists["sovereign_powers"][row["sovereign_id"]], lists["sovereign_prohibitions"][row["sovereign_id"]], row["basis"]) for row in connection.execute("SELECT * FROM sovereigns ORDER BY position")),
            directories=_load_codex_directories(connection),
        )
    finally:
        connection.close()


GOVERNANCE_CODEX = load_governance_codex()
