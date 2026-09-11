"""Directory and registry audit checks for the directory special law (A231/A232/A233)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from governance_rule.execution.codex_repository import codex_version_units

DIRECTORY_TABLES = {
    "fault_code_directory": "fault_code",
    "command_code_directory": "command_code",
    "maintenance_manual_directory": "manual_code",
    "test_flow_directory": "test_flow_code",
    "project_architecture_directory": "architecture_code",
    "special_law_directory": "special_law_code",
    "law_structure_directory": "law_code",
}

KEBAB_IDENTITY_COLUMNS = {
    "command_code_directory": "canonical_command_id",
    "fault_code_directory": "canonical_name",
    "maintenance_manual_directory": "canonical_name",
    "test_flow_directory": "canonical_name",
    "project_architecture_directory": "canonical_name",
    "special_law_directory": "canonical_name",
    "law_structure_directory": "canonical_name",
}

REQUIRED_SCHEMA_TABLES = set(DIRECTORY_TABLES) | {
    "directory_master_catalog",
    "directory_format_contract",
    "directory_coverage_requirements",
    "directory_relationship_requirements",
    "provision_law_classification",
    "seal_manifest",
    "metadata",
}

_UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _codex_connection(root: Path) -> sqlite3.Connection:
    database = root / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
    return sqlite3.connect(f"file:{database.as_posix()}?mode=ro&immutable=1", uri=True)


def _table_rows(connection: sqlite3.Connection, table: str) -> tuple[list[str], list[dict]]:
    columns = [
        column[1] for column in connection.execute(f"PRAGMA table_info({table})")
    ]
    rows = [
        dict(zip(columns, row))
        for row in connection.execute(f"SELECT * FROM {table}")
    ]
    return columns, rows


def check_directory_audit(root: Path, errors: list[str]) -> None:
    """Run the directory special law audit gate (A231 AUDIT-GATE)."""
    checks = (
        check_directory_schemas,
        check_directory_catalog_coverage,
        check_directory_identity_and_format,
        check_provision_classification,
        check_directory_relationships,
        check_directory_mirror_parity,
        check_directory_seal,
    )
    for check in checks:
        try:
            check(root, errors)
        except sqlite3.Error as error:
            errors.append(f"{check.__name__} failed reading the codex: {error}")


def check_directory_schemas(root: Path, errors: list[str]) -> None:
    """Verify every governed directory and registry table exists."""
    with _codex_connection(root) as connection:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    for table in sorted(REQUIRED_SCHEMA_TABLES):
        if table not in existing:
            errors.append(f"governed directory schema is missing: {table}")


def check_directory_catalog_coverage(root: Path, errors: list[str]) -> None:
    """Verify the master catalog covers every directory class and physical table."""
    with _codex_connection(root) as connection:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        catalog = list(
            connection.execute(
                "SELECT directory_code, canonical_name, implementation_state, owner "
                "FROM directory_master_catalog"
            )
        )
        contract_codes = {
            str(row[0])
            for row in connection.execute("SELECT directory_code FROM directory_format_contract")
        }
        catalog_codes = {
            str(row[0])
            for row in connection.execute("SELECT directory_code FROM directory_master_catalog")
        }
    if not catalog:
        errors.append("directory master catalog is empty")
        return
    if contract_codes != catalog_codes:
        errors.append("directory format contracts do not match the master catalog exactly")
    active_tables: set[str] = set()
    for code, canonical, state, owner in catalog:
        if str(owner) != "permission-sovereign":
            errors.append(f"master catalog entry {code} owner is {owner!r}")
        physical = str(canonical).replace("-", "_")
        if state == "active":
            active_tables.add(physical)
            if physical not in existing:
                errors.append(
                    f"active master catalog entry {code} has no physical table"
                )
        elif physical in existing:
            errors.append(
                f"non-active master catalog entry {code} already has a physical table"
            )
    for table in DIRECTORY_TABLES:
        if table not in active_tables:
            errors.append(
                f"governed directory table {table} has no active master catalog entry"
            )


def check_directory_identity_and_format(root: Path, errors: list[str]) -> None:
    """Verify directory identity format, ownership, uniqueness, and versions."""
    with _codex_connection(root) as connection:
        seal_versions = {
            codex_version_units(row[0])
            for row in connection.execute("SELECT version FROM seal_manifest")
        }
        if not seal_versions:
            errors.append("seal manifest is empty")
            return
        current_units = max(seal_versions)
        for table, identity_column in DIRECTORY_TABLES.items():
            _, rows = _table_rows(connection, table)
            identity_values = set()
            for row in rows:
                code = row.get(identity_column)
                if code is None or not _UPPER_SNAKE.match(str(code)):
                    errors.append(
                        f"{table} identity is not uppercase-snake-case: {code!r}"
                    )
                identity_values.add(code)
                kebab = row.get(KEBAB_IDENTITY_COLUMNS[table])
                if kebab and not _KEBAB.match(str(kebab)):
                    errors.append(
                        f"{table} canonical identity is not lower-kebab-case: {kebab!r}"
                    )
                if "owner" in row and row.get("owner") != "permission-sovereign":
                    errors.append(f"{table} owner is not permission-sovereign")
                for column in ("introduced_version", "retired_version"):
                    version = row.get(column)
                    if version is None:
                        continue
                    try:
                        units = codex_version_units(version)
                    except ValueError:
                        errors.append(
                            f"{table} {column} is not a well-formed version: {version!r}"
                        )
                        continue
                    if units > current_units:
                        errors.append(
                            f"{table} {column} exceeds the sealed version: {version}"
                        )
            if len(identity_values) != len(rows):
                errors.append(
                    f"{table} identity column {identity_column} is not unique"
                )


def check_provision_classification(root: Path, errors: list[str]) -> None:
    """Verify every provision maps exactly once to a resolvable classification."""
    provision_types = {
        "principles": "principle",
        "articles": "article",
        "edicts": "edict",
        "sovereigns": "sovereign",
    }
    identity_columns = {
        "principles": "provision_id",
        "articles": "provision_id",
        "edicts": "provision_id",
        "sovereigns": "sovereign_id",
    }
    with _codex_connection(root) as connection:
        classifications = list(
            connection.execute(
                "SELECT provision_type, provision_id, law_code, tier "
                "FROM provision_law_classification"
            )
        )
        classified = {
            (str(provision_type), str(provision_id))
            for provision_type, provision_id, _law_code, _tier in classifications
        }
        expected: set[tuple[str, str]] = set()
        for table, provision_type in provision_types.items():
            expected |= {
                (provision_type, str(row[0]))
                for row in connection.execute(
                    f"SELECT {identity_columns[table]} FROM {table}"
                )
            }
        law_codes = {
            str(row[0])
            for row in connection.execute("SELECT law_code FROM law_structure_directory")
        }
    if len(classified) != len(classifications):
        errors.append("provision classification contains duplicate identities")
    if classified != expected:
        errors.append("provision classification does not map every provision exactly once")
    for provision_type, provision_id, law_code, tier in classifications:
        if str(law_code) not in law_codes:
            errors.append(
                f"classification {provision_type} {provision_id} law_code is unknown: {law_code!r}"
            )
        if tier not in ("main-codex", "subordinate-ordinance", "special-law"):
            errors.append(
                f"classification {provision_type} {provision_id} tier is invalid: {tier!r}"
            )


def check_directory_relationships(root: Path, errors: list[str]) -> None:
    """Verify registered directory references resolve and parent chains stay acyclic."""
    with _codex_connection(root) as connection:
        def identities(table: str, column: str) -> set[str]:
            return {
                str(row[0])
                for row in connection.execute(f"SELECT {column} FROM {table}")
            }

        referenced_pairs = (
            (
                "maintenance_manual_directory",
                "applicable_fault_codes",
                identities("fault_code_directory", "fault_code"),
            ),
            (
                "project_architecture_directory",
                "command_codes",
                identities("command_code_directory", "command_code"),
            ),
            (
                "project_architecture_directory",
                "fault_codes",
                identities("fault_code_directory", "fault_code"),
            ),
            (
                "project_architecture_directory",
                "manual_codes",
                identities("maintenance_manual_directory", "manual_code"),
            ),
            (
                "project_architecture_directory",
                "test_flow_codes",
                identities("test_flow_directory", "test_flow_code"),
            ),
        )
        for source, column, targets in referenced_pairs:
            for row in connection.execute(
                f"SELECT {column} FROM {source} WHERE {column} IS NOT NULL"
            ):
                for value in str(row[0]).split("|"):
                    if value and value not in targets:
                        errors.append(
                            f"{source}.{column} references an unknown code: {value}"
                        )
        provisions = (
            identities("articles", "provision_id")
            | identities("principles", "provision_id")
            | identities("edicts", "provision_id")
            | identities("sovereigns", "sovereign_id")
        )
        for row in connection.execute(
            "SELECT test_flow_code, requirement_ids FROM test_flow_directory "
            "WHERE requirement_ids IS NOT NULL"
        ):
            for value in str(row[1]).split("|"):
                if value and value not in provisions:
                    errors.append(
                        f"test_flow_directory {row[0]} references unknown provision: {value}"
                    )
        for row in connection.execute(
            "SELECT special_law_code, general_provision_links, special_provision_links "
            "FROM special_law_directory"
        ):
            for field in (1, 2):
                if row[field] is None:
                    continue
                for value in str(row[field]).split("|"):
                    if value and value not in provisions:
                        errors.append(
                            f"special_law_directory {row[0]} references unknown provision: {value}"
                        )
        _check_acyclic_parents(
            connection, "law_structure_directory", "law_code", "parent_law_code", errors
        )
        _check_acyclic_parents(
            connection,
            "project_architecture_directory",
            "architecture_code",
            "parent_code",
            errors,
        )


def _check_acyclic_parents(
    connection: sqlite3.Connection,
    table: str,
    identity_column: str,
    parent_column: str,
    errors: list[str],
) -> None:
    records = {
        str(row[0]): row[1]
        for row in connection.execute(
            f"SELECT {identity_column}, {parent_column} FROM {table}"
        )
    }
    for node, parent in records.items():
        if parent is not None and str(parent) not in records:
            errors.append(f"{table} {node} parent is unknown: {parent}")
    visited: set[str] = set()
    for node in records:
        if node in visited:
            continue
        path: set[str] = set()
        current = node
        while current in records and current not in visited:
            if current in path:
                errors.append(f"{table} parent chain contains a cycle at {current}")
                break
            path.add(current)
            parent = records[current]
            if parent is None:
                break
            current = str(parent)
        visited.update(path)


def check_directory_mirror_parity(root: Path, errors: list[str]) -> None:
    """Verify the Chinese mirror carries identical directory and registry rows."""
    mirror_path = root / "governance_rule" / "codex" / "governance_codex.zh-TW.txt"
    try:
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))["tables"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as error:
        errors.append(f"Chinese codex reference is invalid: {error}")
        return
    single_identity = {
        "directory_master_catalog": "directory_code",
        "directory_format_contract": "directory_code",
        "directory_coverage_requirements": "directory_name",
        "directory_relationship_requirements": "relationship_code",
        "seal_manifest": "version",
    }
    with _codex_connection(root) as connection:
        for table, identity_column in DIRECTORY_TABLES.items():
            database_ids = {
                str(row[0])
                for row in connection.execute(f"SELECT {identity_column} FROM {table}")
            }
            mirror_ids = {
                str(row.get(identity_column)) for row in mirror.get(table, [])
            }
            if database_ids != mirror_ids:
                errors.append(f"directory mirror parity mismatch: {table}")
        for table, identity_column in single_identity.items():
            database_ids = {
                str(row[0])
                for row in connection.execute(f"SELECT {identity_column} FROM {table}")
            }
            mirror_ids = {
                str(row.get(identity_column)) for row in mirror.get(table, [])
            }
            if database_ids != mirror_ids:
                errors.append(f"registry mirror parity mismatch: {table}")
        database_classification = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT provision_type, provision_id FROM provision_law_classification"
            )
        }
        mirror_classification = {
            (str(row.get("provision_type")), str(row.get("provision_id")))
            for row in mirror.get("provision_law_classification", [])
        }
        if database_classification != mirror_classification:
            errors.append("classification mirror parity mismatch: provision_law_classification")


def check_directory_seal(root: Path, errors: list[str]) -> None:
    """Verify the current codex version carries a complete seal."""
    with _codex_connection(root) as connection:
        seals = list(
            connection.execute(
                "SELECT version, certification_state, content_root, identity_root, "
                "full_root, history_head FROM seal_manifest"
            )
        )
    if not seals:
        errors.append("seal manifest is empty")
        return
    current = max(seals, key=lambda row: codex_version_units(row[0]))
    version, state, content_root, identity_root, full_root, history_head = current
    if not (content_root and identity_root and full_root and history_head):
        errors.append(f"codex seal {version} has incomplete content roots")
    if not state:
        errors.append(f"codex seal {version} has no certification state")


__all__ = ("check_directory_audit",)