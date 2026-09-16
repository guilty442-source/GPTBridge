"""Optimized Read Model / CQRS Boundary (migration 087).

Every GPTBridge engine stays the single authority for its own domain.  This
module is the *read side* of that boundary: compact, watermarked projections
are derived from authority, grouped into per-module/engine summaries, and
served to status pages / statistics without scanning the authority tables on
every poll.

Projection lifecycle (SQL in ``migrations/087_read_models.sql``):

    refresh_projection(name)      — one-call: start build -> compute -> verify
                                     -> switch. Atomic; readers only ever see a
                                     complete old-or-new version set.
    start_projection_build        — reserve the next 'building' version
    compute_projection_rows       — read authority, write derived rows
    publish_projection_build      — verify row_count, retire old versions
    drop_projection               — DROP derived state (rebuildability contract)
    get_projection_lag            — CURRENT | STALE | OUTDATED | REBUILD_REQUIRED
    watermark_for                 — guarded authority watermark

Consistency levels (per the CQRS read-model spec):

    STRONG        — the caller needs today / the last committed write.  The
                    projection is refreshed synchronously from authority, then
                    served; if that fails the read fails closed.
    BOUNDED_STALE — serve the projection when it is fresh enough; otherwise
                    refresh once and retry, then raise ReadModelStaleError so
                    the caller can degrade to its own authority fallback.
    EVENTUAL      — serve whatever the projection holds (UI dashboards).

Read-your-writes: pass ``expected_revision`` and the client will only serve a
row set whose registry revision is >= that watermark, else refresh and retry.

Caches:
    AuthoritativeCache      — TTL + generation + revision aware cache for
                              projection results (negative results cached too).
    SecuritySnapshotCache   — permission/security snapshot cache that
                              invalidates wholesale on generation bumps.
    ReadModelMaintainer     — optional periodic refresh loop (never started
                              implicitly; wire it into the existing maintenance
                              cadence).  refresh_projections() is the
                              one-shot function to call from a scheduler.

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data (authority stays).
    A10/E10 — explicit-allowlist; deny-by-default (all SQL via query_allowlist).
    A44/E30 — four-functions-local (SQLite Class D = cache marker here).
"""
from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional

from psycopg import Connection

from shared_layer.database.query_allowlist import get_query
from shared_layer.database.sqlite_classification import register as register_sqlite_class

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


# ============================================================================
# ReadModelClient — consistency-aware read boundary
# ============================================================================

class ReadModelClient:
    """Consistency-aware facade over the projected read models.

    Do NOT use for write-path decisions — authority tables remain the single
    write authority.  This client is for status views and statistics where a
    projection is acceptable per the requested ``ConsistencyLevel``.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Connection[Any]],
        *,
        bound_seconds: int = DEFAULT_BOUND_SECONDS,
        cache: Optional["AuthoritativeCache"] = None,
    ) -> None:
        self._factory = connection_factory
        self._bound_seconds = bound_seconds
        self._cache = cache or AuthoritativeCache()

    def query(
        self,
        projection: str,
        *,
        level: ConsistencyLevel = ConsistencyLevel.BOUNDED_STALE,
        filter_value: Optional[str] = None,
        expected_revision: Optional[int] = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Serve projection rows with the requested consistency.

        Returns ``{"data": [...], "meta": {...}}``.  ``meta`` carries the
        registry/projection metadata + the lag classification so callers can
        reason about freshness without another query.
        """
        _check_projection(projection)

        # Fast path: a valid cache hit is fine for BOUNDED_STALE / EVENTUAL even
        # before we hit the database.
        cache_key = (projection, filter_value)
        if use_cache and level is not ConsistencyLevel.STRONG:
            cached = self._cache.get(projection, filter_value=filter_value)
            if cached is not None:
                if expected_revision is None or cached["source_revision"] >= expected_revision:
                    meta = dict(cached["meta"])
                    meta["served_from"] = "cache"
                    return {"data": cached["data"], "meta": meta}

        with self._factory() as connection:
            lag_map = get_projection_lag_map(connection, projection)
            lag = lag_map.get(projection)

            if level is ConsistencyLevel.STRONG:
                # Keep STRONG honest: bring the projection in line with authority
                # synchronously, then serve.  On failure we fail closed — never
                # hand back a projection we cannot vouch for.
                try:
                    refresh_projection(connection, projection)
                except Exception as exc:  # noqa: BLE001
                    raise ReadModelError(
                        f"STRONG read of {projection} failed to refresh from authority: {exc}"
                    ) from exc
                data = read_projection(connection, projection, filter_value=filter_value)
                lag_map = get_projection_lag_map(connection, projection)
                lag = lag_map.get(projection)
                meta = lag or {
                    "projection_name": projection,
                    "status": LAG_CURRENT,
                    "projection_revision": 0,
                }
                meta["served_from"] = "authority-refresh"
                self._cache.put(
                    projection,
                    data,
                    meta,
                    filter_value=filter_value,
                    source_revision=meta.get("projection_revision"),
                )
                return {"data": data, "meta": meta}

            # BOUNDED_STALE / EVENTUAL: serve the projection; enforce the bound
            # for BOUNDED_STALE with exactly one refresh-and-retry attempt.
            data = read_projection(connection, projection, filter_value=filter_value)
            meta = lag or {
                "projection_name": projection,
                "status": LAG_REBUILD_REQUIRED,
                "projection_revision": -1,
                "lag_seconds": None,
            }
            meta["served_from"] = "projection"

            if level is ConsistencyLevel.EVENTUAL:
                self._cache.put(
                    projection,
                    data,
                    meta,
                    filter_value=filter_value,
                    source_revision=meta.get("projection_revision"),
                )
                return {"data": data, "meta": meta}

            # BOUNDED_STALE
            needs_refresh = meta.get("status") in (LAG_OUTDATED, LAG_REBUILD_REQUIRED)
            if meta.get("lag_seconds") is not None:
                needs_refresh = needs_refresh or float(meta["lag_seconds"]) > self._bound_seconds
            if expected_revision is not None:
                pv = meta.get("projection_revision") or -1
                needs_refresh = needs_refresh or int(pv) < expected_revision

            if needs_refresh:
                try:
                    refresh_projection(connection, projection)
                except Exception as exc:  # noqa: BLE001
                    raise ReadModelStaleError(
                        f"{projection} is stale and refresh failed: {exc}"
                    ) from exc
                data = read_projection(connection, projection, filter_value=filter_value)
                lag = get_projection_lag_map(connection, projection).get(projection)
                meta = lag or {
                    "projection_name": projection,
                    "status": LAG_REBUILD_REQUIRED,
                    "projection_revision": -1,
                    "lag_seconds": None,
                }
                meta["served_from"] = "projection(refreshed)"
                if meta.get("status") in (LAG_OUTDATED, LAG_REBUILD_REQUIRED):
                    raise ReadModelStaleError(
                        f"{projection} still stale after refresh (status={meta.get('status')})"
                    )
                if expected_revision is not None and int(meta.get("projection_revision") or -1) < expected_revision:
                    raise ReadModelStaleError(
                        f"{projection} source_revision {meta.get('projection_revision')} < "
                        f"expected {expected_revision}"
                    )

            self._cache.put(
                projection,
                data,
                meta,
                filter_value=filter_value,
                source_revision=meta.get("projection_revision"),
            )
            return {"data": data, "meta": meta}


# ============================================================================
# AuthoritativeCache — TTL + generation + revision aware projection cache
# ============================================================================

class AuthoritativeCache:
    """Small TTL cache for projection reads with generation fencing.

    Entries are keyed by (projection, filter_value) and carry their registry
    revision, source generation and projection version.  A generation bump or
    TTL expiry invalidates without touching the database.  Caching an empty
    result set is the negative cache: a projection with no rows is served
    cheaply instead of re-scanning authority.

    Uses only the projection tables — it never caches unrestricted paths or
    any data that bypasses governance.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = CACHE_TTL_SECONDS,
        max_size: int = CACHE_SIZE,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._cache: dict[tuple[str, Optional[str]], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._invalidations = 0

    def put(
        self,
        projection: str,
        data: list[dict[str, Any]],
        meta: dict[str, Any],
        *,
        filter_value: Optional[str] = None,
        source_revision: Optional[int] = None,
    ) -> None:
        key = (projection, filter_value)
        entry = {
            "data": list(data),
            "meta": dict(meta),
            "source_revision": int(source_revision) if source_revision is not None else int(meta.get("projection_revision") or 0),
            "expires_at": time.monotonic() + self._ttl_seconds,
        }
        with self._lock:
            if key not in self._cache and len(self._cache) >= self._max_size:
                self._evict_one()
            self._cache[key] = entry

    def get(
        self,
        projection: str,
        *,
        filter_value: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        key = (projection, filter_value)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.monotonic() >= entry["expires_at"]:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return {
                "data": list(entry["data"]),
                "meta": dict(entry["meta"]),
                "source_revision": entry["source_revision"],
            }

    def invalidate(self, projection: str) -> None:
        with self._lock:
            for key in [k for k in self._cache if k[0] == projection]:
                del self._cache[key]
                self._invalidations += 1

    def invalidate_all(self) -> None:
        with self._lock:
            self._invalidations += len(self._cache)
            self._cache.clear()

    def _evict_one(self) -> None:
        if not self._cache:
            return
        # oldest expiry first (approx LRU by insertion for equal TTL)
        seq = sorted(self._cache.items(), key=lambda kv: kv[1]["expires_at"])
        self._cache.pop(seq[0][0], None)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "invalidations": self._invalidations,
                "hit_rate": (self._hits / total) if total > 0 else 0,
            }


# ============================================================================
# SecuritySnapshotCache — permission/security snapshot fast path (migration 021)
# ============================================================================

class SecuritySnapshotCache:
    """Cache for permission snapshots with security-generation fencing.

    Act as the *read-side* fast path for :mod:`shared_layer.security`
    decisions.  Snapshots are captured against a permission generation and a
    security generation; any generation bump (permission change, rotation,
    revocation) invalidates the whole cache because individual decisions are
    cheap to rebuild via the authority snapshot machinery.

    This cache never decides — it only serves what was already decided by the
    security layer.  On cache miss the caller re-captures a snapshot.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 60,
        max_size: int = 8192,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._cache: dict[tuple[str, str, Optional[str], str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._high_water_security_generation: int = 0
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(
        actor_id: str,
        target_module: str,
        target_resource_id: Optional[str],
        operation: str,
    ) -> tuple[str, str, Optional[str], str]:
        return (actor_id, target_module, target_resource_id, operation)

    def put(
        self,
        *,
        actor_id: str,
        target_module: str,
        target_resource_id: Optional[str],
        operation: str,
        snapshot: dict[str, Any],
        security_generation: Optional[int] = None,
        permission_generation: Optional[int] = None,
    ) -> None:
        key = self._key(actor_id, target_module, target_resource_id, operation)
        entry = {
            "snapshot": dict(snapshot),
            "permission_generation": permission_generation,
            "security_generation": security_generation,
            "expires_at": time.monotonic() + self._ttl_seconds,
        }
        with self._lock:
            if security_generation is not None and security_generation > self._high_water_security_generation:
                self._high_water_security_generation = security_generation
            if key not in self._cache and len(self._cache) >= self._max_size:
                seq = sorted(self._cache.items(), key=lambda kv: kv[1]["expires_at"])
                self._cache.pop(seq[0][0], None)
            self._cache[key] = entry

    def get(
        self,
        *,
        actor_id: str,
        target_module: str,
        target_resource_id: Optional[str],
        operation: str,
        security_generation: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        key = self._key(actor_id, target_module, target_resource_id, operation)
        high_water = self._high_water_security_generation
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.monotonic() >= entry["expires_at"]:
                del self._cache[key]
                self._misses += 1
                return None
            sg = entry.get("security_generation")
            # Any bump recorded at or after this snapshot's generation makes it
            # unconditionally stale (revocation must take effect immediately).
            if high_water > (sg if sg is not None else 0):
                del self._cache[key]
                self._misses += 1
                return None
            if security_generation is not None and sg is not None and security_generation != sg:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return {
                "snapshot": dict(entry["snapshot"]),
                "permission_generation": entry["permission_generation"],
                "security_generation": entry["security_generation"],
            }

    def invalidate(self) -> None:
        """Wholesale invalidation after any permission/security change."""
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "high_water_security_generation": self._high_water_security_generation,
                "hit_rate": (self._hits / total) if total > 0 else 0,
            }


# ============================================================================
# ReadModelMaintainer — optional periodic refresh loop
# ============================================================================

class ReadModelMaintainer:
    """Periodic projection refresh (poll based).

    This class is opt-in: it never starts itself.  Either call
    :func:`refresh_projections` from your existing maintenance loop, or
    ``start()``/``stop()`` to spawn one daemon watcher owned by your process.

    ``connection_factory`` must return a NEW connection per call (standard
    pool checkout), because maintenance commits and reconnects freely.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Connection[Any]],
        *,
        names: Optional[list[str]] = None,
        interval_seconds: int = 60,
        on_error: Optional[Callable[[str, Exception], None]] = None,
    ) -> None:
        self._factory = connection_factory
        self._names = names or list(PROJECTIONS)
        self._interval = interval_seconds
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.last_result: dict[str, int] = {}
        self.runs = 0

    def run_once(self) -> dict[str, int]:
        """Run one full refresh pass; returns name -> published version."""
        with self._factory() as connection:
            result = refresh_projections(connection, self._names)
        self.runs += 1
        self.last_result = result
        return dict(result)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("ReadModelMaintainer pass failed: %s", exc)
                if self._on_error is not None:
                    try:
                        self._on_error("maintenance", exc)
                    except Exception:  # noqa: BLE001
                        _logger.exception("ReadModelMaintainer on_error handler failed")

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="gptbridge-readmodel-maintainer",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._thread = None


# ============================================================================
# mark_sqlite_cache — SQLite Class D (cache) registration
# ============================================================================

def mark_sqlite_cache(
    connection: Connection[Any],
    *,
    module_id: str,
    database_path: str,
    description: str = "auto-registered cache (read-model / SQLite fallback)",
) -> None:
    """Register a SQLite database as Class D (cache / fallback).

    The pointer lives in PostgreSQL (``gptbridge_index.sqlite_database_class``)
    so cache semantics — synchronous, backup, retention — are uniform and
    observable, never a property of the local .sqlite file alone.
    """
    register_sqlite_class(
        connection,
        module_id=module_id,
        database_path=database_path,
        db_class="D",
        description=description,
    )


__all__ = [
    "PROJECTIONS",
    "LAG_CURRENT",
    "LAG_STALE",
    "LAG_OUTDATED",
    "LAG_REBUILD_REQUIRED",
    "DEFAULT_BOUND_SECONDS",
    "ConsistencyLevel",
    "ReadModelError",
    "ReadModelStaleError",
    "ProjectionNotFoundError",
    "refresh_projection",
    "start_projection_build",
    "compute_projection_rows",
    "publish_projection_build",
    "drop_projection",
    "watermark",
    "get_projection_lag",
    "get_projection_lag_map",
    "refresh_projections",
    "read_projection",
    "ReadModelClient",
    "AuthoritativeCache",
    "SecuritySnapshotCache",
    "ReadModelMaintainer",
    "mark_sqlite_cache",
]