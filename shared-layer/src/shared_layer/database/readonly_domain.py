"""Read-Only Domain (migration 030 + D6).

When integrity/schema drift/authority conflict occurs, a specific data
domain can be switched to read-only, rather than shutting down the entire
system.

Usage:
    from shared_layer.database.readonly_domain import set_readonly, is_readonly

    with connection_manager.connection() as conn:
        set_readonly(conn, domain="central-index", reason="schema-drift",
                     activated_by="watchdog")
        if is_readonly(conn, domain="central-index"):
            # serve read-only traffic only

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist; deny-by-default.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg import Connection

_SET = "SELECT gptbridge_index.set_domain_readonly(%s, %s, %s, %s)"
_IS = "SELECT gptbridge_index.is_domain_readonly(%s)"


def set_readonly(
    connection: Connection[Any],
    *,
    domain: str,
    readonly: bool = True,
    reason: Optional[str] = None,
    activated_by: Optional[str] = None,
) -> None:
    """Switch a domain to read-only (or back to read-write)."""
    connection.execute(_SET, (domain, readonly, reason, activated_by))


def is_readonly(
    connection: Connection[Any],
    *,
    domain: str,
) -> bool:
    """Check if a domain is currently read-only."""
    row = connection.execute(_IS, (domain,)).fetchone()
    return bool(row and row[0])


__all__ = ["set_readonly", "is_readonly"]
