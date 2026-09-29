"""Read model core: projection constants, errors, lifecycle wrappers (migration 087)."""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Optional

from psycopg import Connection

from shared_layer.database.query_allowlist import get_query

_logger = logging.getLogger("gptbridge.readmodel")

PROJECTIONS: tuple[str, ...] = (
    "resource_summary",
    "module_status_summary",
    "transport_status_summary",
    "rag_status_summary",
    "database_status_snapshot",
)

LAG_CURRENT = "CURRENT"
LAG_STALE = "STALE"
LAG_OUTDATED = "OUTDATED"
LAG_REBUILD_REQUIRED = "REBUILD_REQUIRED"

DEFAULT_BOUND_SECONDS = 30
CACHE_TTL_SECONDS = 15
CACHE_SIZE = 4096


class ConsistencyLevel(str, Enum):
    """Read consistency levels for the projection boundary."""

    STRONG = "strong"
    BOUNDED_STALE = "bounded_stale"
    EVENTUAL = "eventual"


class ReadModelError(RuntimeError):
    """Base error for the read model layer."""


class ReadModelStaleError(ReadModelError):
    """The projection could not be brought within the requested bound.

    Raise this after one refresh-and-retry attempt so callers can degrade to
    their own authority read.  Never fails silently.
    """


class ProjectionNotFoundError(ReadModelError):
    """The projection name is not a registered read model."""


def _check_projection(name: str) -> None:
    if name not in PROJECTIONS:
        raise ProjectionNotFoundError(
            f"unknown projection: {name!r}. Known: {', '.join(PROJECTIONS)}"
        )


def _select(connection: Connection[Any], sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _scalar(connection: Connection[Any], sql: str, params: tuple[Any, ...]) -> Any:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


# ============================================================================
# Projection lifecycle (thin wrappers over the migration SQL functions)
# ============================================================================

def refresh_projection(connection: Connection[Any], name: str) -> int:
    """Refresh one projection now. Returns the published projection version."""
    _check_projection(name)
    result = _scalar(connection, get_query("readmodel.refresh"), (name,))
    return int(result)


def start_projection_build(connection: Connection[Any], name: str) -> int:
    """Reserve the next 'building' version for a long-running rebuild."""
    _check_projection(name)
    result = _scalar(connection, get_query("readmodel.start_build"), (name,))
    return int(result)


def compute_projection_rows(connection: Connection[Any], name: str, version: int) -> int:
    """Compute the derived rows for a build version. Returns inserted rows."""
    _check_projection(name)
    result = _scalar(connection, get_query("readmodel.compute"), (name, int(version)))
    return int(result)


def publish_projection_build(connection: Connection[Any], name: str, version: int) -> int:
    """Verify + switch the active version and retire older rows."""
    _check_projection(name)
    result = _scalar(connection, get_query("readmodel.publish"), (name, int(version)))
    return int(result)


def drop_projection(connection: Connection[Any], name: str) -> int:
    """DROP derived state for a projection (rebuildability contract)."""
    _check_projection(name)
    result = _scalar(connection, get_query("readmodel.drop"), (name,))
    return int(result)


def watermark(connection: Connection[Any], name: str) -> dict[str, Any]:
    """Guarded authority watermark for a projection."""
    _check_projection(name)
    value = _scalar(connection, get_query("readmodel.watermark"), (name,))
    return value if isinstance(value, dict) else {"source_revision": 0, "source_generation": None, "unit": "revision"}


def get_projection_lag(
    connection: Connection[Any],
    name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Lag / status for one projection (or all when ``name`` is None)."""
    if name is not None:
        _check_projection(name)
    return _select(connection, get_query("readmodel.lag"), (name,))


def get_projection_lag_map(
    connection: Connection[Any],
    name: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """Same as :func:`get_projection_lag` but keyed by projection name."""
    return {row["projection_name"]: row for row in get_projection_lag(connection, name)}


def refresh_projections(
    connection: Connection[Any],
    names: Optional[list[str]] = None,
) -> dict[str, int]:
    """Refresh each projection in one commit cycle. Returns name -> version."""
    selected = [n for n in (names or list(PROJECTIONS))]
    for n in selected:
        _check_projection(n)
    result: dict[str, int] = {}
    for n in selected:
        try:
            result[n] = refresh_projection(connection, n)
        except Exception as exc:  # noqa: BLE001 — a broken projection must not
            # kill the whole maintenance pass; record and continue.
            _logger.warning("refresh_projection(%s) failed: %s", n, exc)
            result[n] = -1
    return result


# ============================================================================
# Read model data access (allowlisted SELECT templates)
# ============================================================================

def _fetch_rows(connection: Connection[Any], projection: str, filter_value: Optional[str]) -> list[dict[str, Any]]:
    _check_projection(projection)
    if projection == "resource_summary":
        sql = get_query("readmodel.rows.resource_summary")
        return _select(connection, sql, (projection, filter_value, filter_value))
    if projection == "module_status_summary":
        sql = get_query("readmodel.rows.module_status_summary")
        return _select(connection, sql, (projection, filter_value, filter_value))
    if projection == "transport_status_summary":
        sql = get_query("readmodel.rows.transport_status_summary")
        return _select(connection, sql, (projection, filter_value, filter_value))
    if projection == "rag_status_summary":
        sql = get_query("readmodel.rows.rag_status_summary")
        return _select(connection, sql, (projection, filter_value, filter_value))
    if projection == "database_status_snapshot":
        sql = get_query("readmodel.rows.database_status_snapshot")
        return _select(connection, sql, (projection,))
    raise ProjectionNotFoundError(projection)


def read_projection(
    connection: Connection[Any],
    projection: str,
    *,
    filter_value: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read the *active* rows of a projection.

    ``filter_value`` narrows per-row scope: module_id for the module/rag
    summaries, resource_type for resource_summary, state for transport.
    """
    return _fetch_rows(connection, projection, filter_value)
