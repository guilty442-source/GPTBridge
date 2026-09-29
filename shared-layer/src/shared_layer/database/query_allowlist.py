"""PostgreSQL Query Allowlist (A10/E10 + A8/E21).

Runtime code routes all SQL through fixed query templates registered here.
Modules must not submit free-form SQL to PostgreSQL.  Each template is
identified by a stable key; the actual SQL is stored centrally so it can
be audited, parameterized, and version-controlled.

Usage:
    from shared_layer.database.query_allowlist import QUERY_TEMPLATES, get_query

    sql = get_query("resource.get_by_id")
    connection.execute(sql, (resource_id,))

The allowlist is a frozen dict — it cannot be mutated at runtime.  New
queries must be added here and reviewed before deployment.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


from .query_allowlist_templates_governance import TEMPLATES_GOVERNANCE
from .query_allowlist_templates_index import TEMPLATES_INDEX
from .query_allowlist_templates_ops import TEMPLATES_OPS


_TEMPLATES: dict[str, str] = {
    **TEMPLATES_INDEX,
    **TEMPLATES_OPS,
    **TEMPLATES_GOVERNANCE,
}

QUERY_TEMPLATES: Mapping[str, str] = MappingProxyType(_TEMPLATES)


def get_query(key: str) -> str:
    """Get a query template by key.  Raises KeyError if not registered."""
    if key not in QUERY_TEMPLATES:
        raise KeyError(
            f"QUERY_NOT_ALLOWLISTED: '{key}' is not in the query allowlist. "
            f"Add it to shared_layer.database.query_allowlist.QUERY_TEMPLATES."
        )
    return QUERY_TEMPLATES[key]


def is_allowlisted(key: str) -> bool:
    """Check if a query key is in the allowlist."""
    return key in QUERY_TEMPLATES


def validate_query(sql: str) -> tuple[bool, str | None]:
    """Validate that a raw SQL string matches an allowlisted template.

    Returns (is_valid, matched_key_or_reason).
    """
    normalized = " ".join(sql.split())
    for key, template in QUERY_TEMPLATES.items():
        if " ".join(template.split()) == normalized:
            return True, key
    return False, "SQL does not match any allowlisted template"


__all__ = [
    "QUERY_TEMPLATES",
    "get_query",
    "is_allowlisted",
    "validate_query",
]
