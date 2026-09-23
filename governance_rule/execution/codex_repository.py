"""Read-only adapter for the authoritative PostgreSQL Governance Codex."""

from __future__ import annotations

import sqlite3
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Iterator

from psycopg.rows import dict_row

from governance_rule.execution.codex_postgresql import (
    CODEX_AUTHORITY_URI,
    CODEX_SCHEMA,
    authority_state,
    readonly_connection,
)


CODEX_VERSION_UNIT: Final[int] = 100_000
# Versions are either the legacy ``integer-dot-five-decimal-digits`` form or
# a UTC timestamp (ISO 8601).  Timestamp versions are represented as epoch
# seconds, which are far above legacy unit counts, so integer ordering and
# comparisons keep working across both eras without floating point.
TIMESTAMP_VERSION_THRESHOLD: Final[int] = 10_000_000


def _timestamp_version_units(text: str) -> int:
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def codex_version_units(value: object) -> int:
    """Convert a codex version to integer units.

    Accepts the legacy ``integer-dot-five-decimal-digits`` form (e.g.
    ``1.41600`` -> ``141600``) or a UTC timestamp (ISO 8601, e.g.
    ``2026-09-13T15:50:00Z``) -> epoch seconds.  Integer-only arithmetic is
    preserved for both forms.
    """
    text = str(value or "").strip()
    whole, separator, fraction = text.partition(".")
    if (
        separator
        and whole.isdigit()
        and fraction.isdigit()
        and len(fraction) == 5
    ):
        return int(whole) * CODEX_VERSION_UNIT + int(fraction)
    try:
        return _timestamp_version_units(text)
    except (TypeError, ValueError):
        raise ValueError(
            f"codex version must be integer-dot-five-decimal-digits or a "
            f"UTC timestamp: {value!r}"
        )


def format_codex_version(version: int) -> str:
    """Render integer units as a legacy version or a UTC timestamp."""
    if version >= TIMESTAMP_VERSION_THRESHOLD:
        return datetime.fromtimestamp(version, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
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


CODEX_DATABASE_PATH = (
    Path(__file__).resolve().parents[1] / "codex" / "data" / "governance_codex.sqlite3"
)
CODEX_DATABASE = CODEX_DATABASE_PATH


class _PostgresCodexCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def __iter__(self):
        return iter(self._cursor)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def execute(self, statement: str, parameters=()):
        self._cursor.execute(_translate_query(statement), parameters)
        return self


class _PostgresCodexConnection:
    """Small DB-API compatibility surface for certified Codex readers."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, statement: str, parameters=()):
        pragma = re.match(r"\s*PRAGMA\s+table_info\(([^)]+)\)\s*$", statement, re.I)
        if pragma:
            table = pragma.group(1).strip().strip('"')
            cursor = self._connection.execute(
                "SELECT ordinal_position - 1, column_name, data_type, "
                "CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END, column_default, "
                "CASE WHEN column_name IN ("
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                "ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                "WHERE i.indrelid=(%s || '.' || %s)::regclass AND i.indisprimary"
                ") THEN 1 ELSE 0 END "
                "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position",
                (CODEX_SCHEMA, table, CODEX_SCHEMA, table),
            )
            return _PostgresCodexCursor(cursor)
        cursor = self._connection.execute(_translate_query(statement), parameters)
        return _PostgresCodexCursor(cursor)

    def cursor(self):
        return _PostgresCodexCursor(self._connection.cursor())


def _translate_query(statement: str) -> str:
    translated = statement
    if "sqlite_master" in translated:
        translated = translated.replace("SELECT name FROM sqlite_master", "SELECT table_name FROM information_schema.tables")
        translated = translated.replace("type='table'", f"table_type='BASE TABLE' AND table_schema='{CODEX_SCHEMA}'")
        translated = translated.replace("name LIKE", "table_name LIKE")
        translated = translated.replace("ORDER BY name", "ORDER BY table_name")
    return translated.replace("?", "%s")


@contextmanager
def codex_readonly_connection(
    path: Path | str = CODEX_DATABASE_PATH,
) -> Iterator[_PostgresCodexConnection]:
    """A279 governed repository connection for certified tooling.

    Certified tooling (the governance audit) may read the official machine
    codex through this governed repository interface; viewers and runtime
    consumers must instead enter through ``governance-codex://official``
    (``codex_official`` / ``codex_session``).  The connection is opened
    read-only/immutable, held only for the ``with`` block, and always
    closed on exit — it never grants file mutation authority.
    """
    del path
    with readonly_connection() as connection:
        yield _PostgresCodexConnection(connection)


def _load_codex_tables(
    connection,
    suffix: str,
) -> dict[str, tuple[CodexDirectoryRow, ...]]:
    """Read every authoritative ``*{suffix}`` table read-only.

    Directories (``*_directory``) are permission-sovereign-owned identity
    registries; registries (``*_registry``) are the machine authority for
    hierarchy/assignment/supersession bindings (A334).  Both are registered
    non-content identity/status/binding data — never written here.
    """
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' "
            "AND table_name LIKE %s ORDER BY table_name",
            (CODEX_SCHEMA, f"%{suffix}"),
        )
    ]
    directories: dict[str, tuple[CodexDirectoryRow, ...]] = {}
    for table in tables:
        column_names = [
            column[0]
            for column in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (CODEX_SCHEMA, table),
            )
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


def _load_codex_directories(
    connection: sqlite3.Connection,
) -> dict[str, tuple[CodexDirectoryRow, ...]]:
    return _load_codex_tables(connection, "_directory")


def _load_codex_registries(
    connection: sqlite3.Connection,
) -> dict[str, tuple[CodexDirectoryRow, ...]]:
    return _load_codex_tables(connection, "_registry")


_codex_cache: dict[tuple[str, int, int], GovernanceCodex] = {}
_resolved_codex_paths: dict[str, str] = {}


def _resolved_path_key(path: Path) -> str:
    """Memoize ``Path.resolve()`` ??it is realpath-syscall heavy on Windows.

    Status surfaces rebuild the sovereign tree per request and each
    ``decision_basis`` call reaches this function; resolving the same
    module-constant path on every call saturated the backend event loop.
    """

    raw = str(path)
    resolved = _resolved_codex_paths.get(raw)
    if resolved is None:
        resolved = str(Path(raw).resolve())
        _resolved_codex_paths[raw] = resolved
    return resolved


def load_governance_codex(path: Path = CODEX_DATABASE_PATH) -> GovernanceCodex:
    """Load the authoritative codex, cached by PostgreSQL authority generation.

    Status surfaces rebuild the sovereign tree per request and each
    ``decision_basis`` call re-reads all codex tables; caching keyed on
    ``st_mtime_ns`` keeps dynamic amendments visible (a codex write changes
    the mtime) while collapsing repeated full loads within one report.
    """
    del path
    state = authority_state()
    key = (
        CODEX_AUTHORITY_URI,
        hash(str(state.get("codex_version", ""))),
        hash(str(state.get("source_sha256", ""))),
    )
    if key is not None:
        cached = _codex_cache.get(key)
        if cached is not None:
            return cached
    codex = _load_governance_codex()
    if key is not None:
        _codex_cache.clear()
        _codex_cache[key] = codex
    return codex


def _normalized_codex_schema(schema: str, codex_version: int) -> str:
    """Keep the declared schema bound to the current codex version.

    The stored schema embeds the version it was written with; the codex text
    is updated independently (timestamp version), so the loader re-anchors the
    version suffix instead of trusting a stale copy.
    """
    declared = str(schema or "").strip()
    base = declared.rsplit("-v", 1)[0].strip() if "-v" in declared else ""
    if not base:
        base = "gptbridge-governance-codex"
    return f"{base}-v{format_codex_version(codex_version)}"


def _dict_rows(connection, statement: str):
    with connection.cursor(row_factory=dict_row) as cursor:
        return tuple(cursor.execute(statement).fetchall())


def _load_governance_codex(path: Path = CODEX_DATABASE_PATH) -> GovernanceCodex:
    del path
    with readonly_connection() as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
        with connection.cursor(row_factory=dict_row) as cursor:
            preamble = cursor.execute("SELECT * FROM preamble WHERE id=1").fetchone()
            savings = cursor.execute("SELECT * FROM savings WHERE id=1").fetchone()
        lists = {
            table: {
                key: tuple(row[0] for row in connection.execute(
                    f"SELECT value FROM {table} WHERE sovereign_id=%s ORDER BY position",
                    (key,),
                ))
                for key, in connection.execute("SELECT sovereign_id FROM sovereigns")
            }
            for table in ("sovereign_duties", "sovereign_powers", "sovereign_prohibitions")
        }
        codex_version = codex_version_units(metadata["codex_version"])
        return GovernanceCodex(
            schema=_normalized_codex_schema(metadata["schema"], codex_version),
            codex_version=codex_version,
            preamble=CodexPreamble(*(preamble[key] for key in ("title", "authority_rank", "issuance", "binding_scope"))),
            sections=tuple(CodexSection(row["section_index"], row["title"], row["summary"]) for row in _dict_rows(connection, "SELECT * FROM sections ORDER BY position")),
            principles=tuple(CodexPrinciple(row["provision_id"], row["statement"], bool(row["binding"])) for row in _dict_rows(connection, "SELECT * FROM principles ORDER BY position")),
            articles=tuple(CodexArticle(*(row[key] for key in ("provision_id", "section_index", "subject", "rule", "prohibition", "exception"))) for row in _dict_rows(connection, "SELECT * FROM articles ORDER BY position")),
            edicts=tuple(CodexEdict(*(row[key] for key in ("provision_id", "area", "edict", "immutability"))) for row in _dict_rows(connection, "SELECT * FROM edicts ORDER BY position")),
            savings=CodexSavings(*(savings[key] for key in ("mutability", "function", "amendment", "overriding_authority", "interpretation", "conflict_resolution"))),
            sovereigns=tuple(CodexSovereign(row["sovereign_id"], row["name"], row["area"], row["rank"], lists["sovereign_duties"][row["sovereign_id"]], lists["sovereign_powers"][row["sovereign_id"]], lists["sovereign_prohibitions"][row["sovereign_id"]], row["basis"]) for row in _dict_rows(connection, "SELECT * FROM sovereigns ORDER BY position")),
            directories=_load_codex_directories(connection),
            registries=_load_codex_registries(connection),
        )


def __getattr__(name: str):
    """Lazy compatibility for certified tooling only (A279/A435).

    ``GOVERNANCE_CODEX`` was once an eager import-time snapshot — an
    uncontrolled full codex read.  It is no longer bound eagerly; this
    hook resolves it on demand through the same governed repository load
    so certified tooling and test discovery keep working.  Runtime
    viewers must not use it: they enter through
    ``governance-codex://official`` (``codex_official``/``codex_session``)
    with identity, purpose, scope, session and audit.
    """
    if name == "GOVERNANCE_CODEX":
        return load_governance_codex()
    if name == "prewarm_codex_cache":
        return prewarm_codex_cache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_CODEX_PREWARMED = False


def prewarm_codex_cache() -> bool:
    """Pre-warm the codex cache by loading it in the background.

    This should be called early in startup (e.g., during boot_core phases)
    so the codex is cached and ready when the startup sovereign needs it.
    Returns True if prewarming was initiated or cache was already warm.
    """
    global _CODEX_PREWARMED
    if _CODEX_PREWARMED:
        return True
    try:
        # Trigger cache load in background thread
        import threading

        def _load():
            try:
                load_governance_codex()
            except Exception:
                pass

        thread = threading.Thread(target=_load, daemon=True, name="codex-prewarm")
        thread.start()
        _CODEX_PREWARMED = True
        return True
    except Exception:
        return False
