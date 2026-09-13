"""Read-only adapter for the authoritative Governance Codex (Chinese source).

法典依據:
- A1: governance-rule is the sole adjudication basis; pure declaration; no power.
- A74: ALL-CODEX-CITATION: enter-through-governance-codex://official

The authoritative codex is the Chinese text file
``governance_rule/codex/governance_codex.zh-TW.txt`` — a sealed JSON
document containing every codex table.  This module parses that file
read-only and exposes the same ``GovernanceCodex`` data model that the
former SQLite adapter provided, so downstream callers are unaffected by
the storage change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final


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
    registries: dict[str, tuple[CodexDirectoryRow, ...]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Source path — the authoritative Chinese codex text file (sealed JSON).
# ---------------------------------------------------------------------------

CODEX_SOURCE_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "codex" / "governance_codex.zh-TW.txt"
)
# Backward-compatible aliases (formerly pointed at the SQLite database).
CODEX_DATABASE_PATH = CODEX_SOURCE_PATH
CODEX_DATABASE = CODEX_SOURCE_PATH


# ---------------------------------------------------------------------------
# JSON table reader — replaces the former SQLite connection.
# ---------------------------------------------------------------------------

_codex_json_cache: dict[tuple[str, int, int], dict[str, Any]] = {}


def _load_codex_json(path: Path = CODEX_SOURCE_PATH) -> dict[str, Any]:
    """Load and cache the raw codex JSON document keyed on mtime+size."""
    try:
        stat_result = Path(path).stat()
        key = (str(Path(path).resolve()), stat_result.st_mtime_ns, stat_result.st_size)
    except OSError:
        key = None
    if key is not None:
        cached = _codex_json_cache.get(key)
        if cached is not None:
            return cached
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if key is not None:
        _codex_json_cache.clear()
        _codex_json_cache[key] = data
    return data


def codex_table_names(path: Path = CODEX_SOURCE_PATH) -> set[str]:
    """Return the set of all table names in the codex JSON."""
    return set(_load_codex_json(path).get("tables", {}).keys())


def codex_table_rows(table: str, path: Path = CODEX_SOURCE_PATH) -> list[dict[str, Any]]:
    """Return all rows of a codex table as a list of dicts (column -> value)."""
    tables = _load_codex_json(path).get("tables", {})
    return list(tables.get(table, []))


def codex_table_columns(table: str, path: Path = CODEX_SOURCE_PATH) -> list[str]:
    """Return the column names of a codex table (from the first row)."""
    rows = codex_table_rows(table, path)
    if not rows:
        return []
    return list(rows[0].keys())


def _load_codex_directories(tables: dict[str, Any]) -> dict[str, tuple[CodexDirectoryRow, ...]]:
    """Read every authoritative ``*_directory`` table (A223-A227/A228) read-only."""
    directories: dict[str, tuple[CodexDirectoryRow, ...]] = {}
    for table_name, rows in tables.items():
        if not table_name.endswith("_directory"):
            continue
        directory_rows: list[CodexDirectoryRow] = []
        for row in rows:
            fields = tuple((key, str(value)) for key, value in row.items())
            directory_rows.append(CodexDirectoryRow(fields))
        directories[table_name] = tuple(directory_rows)
    return directories


def _load_codex_registries(tables: dict[str, Any]) -> dict[str, tuple[CodexDirectoryRow, ...]]:
    """Read every authoritative ``*_registry`` table (A334) read-only."""
    registries: dict[str, tuple[CodexDirectoryRow, ...]] = {}
    for table_name, rows in tables.items():
        if not table_name.endswith("_registry"):
            continue
        registry_rows: list[CodexDirectoryRow] = []
        for row in rows:
            fields = tuple((key, str(value)) for key, value in row.items())
            registry_rows.append(CodexDirectoryRow(fields))
        registries[table_name] = tuple(registry_rows)
    return registries


_codex_cache: dict[tuple[str, int, int], GovernanceCodex] = {}


def load_governance_codex(path: Path = CODEX_SOURCE_PATH) -> GovernanceCodex:
    """Load the authoritative codex, cached on the source file's mtime."""
    try:
        stat_result = Path(path).stat()
        key = (str(Path(path).resolve()), stat_result.st_mtime_ns, stat_result.st_size)
    except OSError:
        key = None
    if key is not None:
        cached = _codex_cache.get(key)
        if cached is not None:
            return cached
    codex = _load_governance_codex(path)
    if key is not None:
        _codex_cache.clear()
        _codex_cache[key] = codex
    return codex


def _load_governance_codex(path: Path = CODEX_SOURCE_PATH) -> GovernanceCodex:
    raw = _load_codex_json(path)
    tables: dict[str, Any] = raw.get("tables", {})

    # metadata — key/value pairs
    metadata = {row["key"]: row["value"] for row in tables.get("metadata", [])}

    # preamble — single row
    preamble_rows = tables.get("preamble", [])
    preamble_row = preamble_rows[0] if preamble_rows else {}

    # savings — single row
    savings_rows = tables.get("savings", [])
    savings_row = savings_rows[0] if savings_rows else None

    # sovereign list tables (duties/powers/prohibitions)
    def _sovereign_list(table_name: str) -> dict[str, tuple[str, ...]]:
        result: dict[str, tuple[str, ...]] = {}
        for row in tables.get(table_name, []):
            sid = row["sovereign_id"]
            result.setdefault(sid, ())
            result[sid] = (*result[sid], row["value"])
        return result

    duties = _sovereign_list("sovereign_duties")
    powers = _sovereign_list("sovereign_powers")
    prohibitions = _sovereign_list("sovereign_prohibitions")

    return GovernanceCodex(
        schema=metadata.get("schema", ""),
        codex_version=codex_version_units(metadata.get("codex_version", "1.00000")),
        preamble=CodexPreamble(
            preamble_row.get("title", ""),
            preamble_row.get("authority_rank", ""),
            preamble_row.get("issuance", ""),
            preamble_row.get("binding_scope", ""),
        ),
        sections=tuple(
            CodexSection(
                str(row.get("section_index", "")),
                row.get("title", ""),
                row.get("summary", ""),
            )
            for row in tables.get("sections", [])
        ),
        principles=tuple(
            CodexPrinciple(
                row.get("provision_id", ""),
                row.get("statement", ""),
                bool(row.get("binding", 0)),
            )
            for row in tables.get("principles", [])
        ),
        articles=tuple(
            CodexArticle(
                row.get("provision_id", ""),
                row.get("section_index", ""),
                row.get("subject", ""),
                row.get("rule", ""),
                row.get("prohibition", ""),
                row.get("exception", ""),
            )
            for row in tables.get("articles", [])
        ),
        edicts=tuple(
            CodexEdict(
                row.get("provision_id", ""),
                row.get("area", ""),
                row.get("edict", ""),
                row.get("immutability", ""),
            )
            for row in tables.get("edicts", [])
        ),
        savings=CodexSavings(
            savings_row.get("mutability", ""),
            savings_row.get("function", ""),
            savings_row.get("amendment", ""),
            savings_row.get("overriding_authority", ""),
            savings_row.get("interpretation", ""),
            savings_row.get("conflict_resolution", ""),
        ) if savings_row else None,
        sovereigns=tuple(
            CodexSovereign(
                row.get("sovereign_id", ""),
                row.get("name", ""),
                row.get("area", ""),
                row.get("rank", ""),
                duties.get(row.get("sovereign_id", ""), ()),
                powers.get(row.get("sovereign_id", ""), ()),
                prohibitions.get(row.get("sovereign_id", ""), ()),
                row.get("basis", ""),
            )
            for row in tables.get("sovereigns", [])
        ),
        directories=_load_codex_directories(tables),
        registries=_load_codex_registries(tables),
    )


GOVERNANCE_CODEX = load_governance_codex()
