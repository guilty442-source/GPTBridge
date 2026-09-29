"""ReadModelClient — consistency-aware read boundary (migration 087)."""
from __future__ import annotations

from typing import Any, Callable, Optional

from psycopg import Connection

from .readmodel_cache import AuthoritativeCache
from .readmodel_core import (
    DEFAULT_BOUND_SECONDS,
    LAG_CURRENT,
    LAG_OUTDATED,
    LAG_REBUILD_REQUIRED,
    ConsistencyLevel,
    ReadModelError,
    ReadModelStaleError,
    _check_projection,
    get_projection_lag_map,
    read_projection,
    refresh_projection,
)


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
