"""Canonical live-schema hash — SQL_DDL_CANONICALIZATION_V1 (A502).

Computes a deterministic SHA-256 fingerprint of the live PostgreSQL
schema surface for every governed (non-system) schema.  The recipe is
the codex contract ``SQL_DDL_CANONICALIZATION_V1``:

- identifier_normalization: lowercase-unquoted + preserve-quoted
  (information_schema / pg_catalog already expose canonical casing;
  identifiers are never re-cased here)
- constraint_representation: sorted-by-type-name-definition
- function_definition: pg_get_functiondef-normalized
- index_expression: pg_get_indexdef-normalized
- default_expression: pg_get_expr-normalized
- rls_policy: qual + with_check + roles + command
- trigger_definition: pg_get_triggerdef-normalized
- extension_owned_object_policy: objects owned by a registered
  extension are excluded
- hash_algorithm: SHA-256 over sorted-key canonical JSON

This is the single canonical recipe: the baseline re-anchor runner,
the A502 evaluator evidence path and any future drift gate must all
use ``compute_canonical_schema_hash`` rather than local rewrites.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg

_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")


def _rows(cur: psycopg.Cursor, sql: str) -> list[dict[str, Any]]:
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def compute_canonical_schema_hash(conn: psycopg.Connection) -> dict[str, Any]:
    """Return the canonical live-schema fingerprint.

    Result keys:
      schema_hash   — SHA-256 hexdigest of the canonical descriptor
      object_counts — per-class object counts (audit aid)
      schemas       — schemas included in scope
    """
    with conn.cursor() as cur:
        schemas = [
            r["schema_name"]
            for r in _rows(
                cur,
                """
                SELECT schema_name FROM information_schema.schemata
                WHERE schema_name NOT LIKE 'pg\\_%'
                  AND schema_name <> 'information_schema'
                ORDER BY schema_name
                """,
            )
        ]

        columns = _rows(
            cur,
            """
            SELECT c.table_schema, c.table_name, c.column_name,
                   c.data_type, c.is_nullable, c.ordinal_position,
                   pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS default_expression
            FROM information_schema.columns c
            LEFT JOIN pg_catalog.pg_attribute a
              ON a.attrelid = (quote_ident(c.table_schema) || '.' ||
                               quote_ident(c.table_name))::regclass
             AND a.attname = c.column_name
            LEFT JOIN pg_catalog.pg_attrdef ad
              ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
            WHERE c.table_schema NOT LIKE 'pg\\_%'
              AND c.table_schema <> 'information_schema'
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """,
        )

        constraints = _rows(
            cur,
            """
            SELECT n.nspname AS schema_name, t.relname AS table_name,
                   c.contype AS constraint_type, c.conname AS constraint_name,
                   pg_catalog.pg_get_constraintdef(c.oid) AS definition
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname NOT LIKE 'pg\\_%'
              AND n.nspname <> 'information_schema'
              AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_depend d
                    JOIN pg_catalog.pg_extension e ON e.oid = d.refobjid
                    WHERE d.objid = c.oid)
            ORDER BY c.contype, c.conname,
                     pg_catalog.pg_get_constraintdef(c.oid)
            """,
        )

        indexes = _rows(
            cur,
            """
            SELECT n.nspname AS schema_name, t.relname AS table_name,
                   i.relname AS index_name,
                   pg_catalog.pg_get_indexdef(i.oid) AS index_definition
            FROM pg_catalog.pg_index x
            JOIN pg_catalog.pg_class i ON i.oid = x.indexrelid
            JOIN pg_catalog.pg_class t ON t.oid = x.indrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname NOT LIKE 'pg\\_%'
              AND n.nspname <> 'information_schema'
              AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_constraint c
                    WHERE c.conindid = i.oid)
            ORDER BY n.nspname, t.relname, i.relname
            """,
        )

        functions = _rows(
            cur,
            """
            SELECT n.nspname AS schema_name, p.proname AS function_name,
                   pg_catalog.pg_get_functiondef(p.oid) AS definition
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname NOT LIKE 'pg\\_%'
              AND n.nspname <> 'information_schema'
              AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_depend d
                    JOIN pg_catalog.pg_extension e ON e.oid = d.refobjid
                    WHERE d.objid = p.oid)
            ORDER BY n.nspname, p.proname
            """,
        )

        triggers = _rows(
            cur,
            """
            SELECT n.nspname AS schema_name, t.relname AS table_name,
                   g.tgname AS trigger_name,
                   pg_catalog.pg_get_triggerdef(g.oid) AS definition
            FROM pg_catalog.pg_trigger g
            JOIN pg_catalog.pg_class t ON t.oid = g.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname NOT LIKE 'pg\\_%'
              AND n.nspname <> 'information_schema'
              AND NOT g.tgisinternal
            ORDER BY n.nspname, t.relname, g.tgname
            """,
        )

        policies = _rows(
            cur,
            """
            SELECT schemaname AS schema_name, tablename AS table_name,
                   policyname AS policy_name, cmd AS command, roles,
                   qual, with_check
            FROM pg_catalog.pg_policies
            WHERE schemaname NOT LIKE 'pg\\_%'
              AND schemaname <> 'information_schema'
            ORDER BY schemaname, tablename, policyname
            """,
        )

        sequences = _rows(
            cur,
            """
            SELECT sequence_schema AS schema_name, sequence_name,
                   data_type, start_value, minimum_value, maximum_value,
                   increment, cycle_option
            FROM information_schema.sequences
            WHERE sequence_schema NOT LIKE 'pg\\_%'
              AND sequence_schema <> 'information_schema'
            ORDER BY sequence_schema, sequence_name
            """,
        )

        views = _rows(
            cur,
            """
            SELECT table_schema AS schema_name, table_name AS view_name,
                   pg_catalog.pg_get_viewdef(
                     (quote_ident(table_schema) || '.' ||
                      quote_ident(table_name))::regclass) AS definition
            FROM information_schema.views
            WHERE table_schema NOT LIKE 'pg\\_%'
              AND table_schema <> 'information_schema'
            ORDER BY table_schema, table_name
            """,
        )

        extensions = _rows(
            cur,
            """
            SELECT extname AS extension_name, extversion AS version
            FROM pg_catalog.pg_extension
            ORDER BY extname
            """,
        )

    descriptor = {
        "recipe": "SQL_DDL_CANONICALIZATION_V1",
        "schemas": schemas,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "functions": functions,
        "triggers": triggers,
        "rls_policies": policies,
        "sequences": sequences,
        "views": views,
        "extensions": extensions,
    }
    canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "schema_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "object_counts": {
            "schemas": len(schemas),
            "columns": len(columns),
            "constraints": len(constraints),
            "indexes": len(indexes),
            "functions": len(functions),
            "triggers": len(triggers),
            "rls_policies": len(policies),
            "sequences": len(sequences),
            "views": len(views),
            "extensions": len(extensions),
        },
        "schemas": schemas,
    }
