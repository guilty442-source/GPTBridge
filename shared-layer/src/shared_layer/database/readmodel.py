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


from .readmodel_cache import AuthoritativeCache, SecuritySnapshotCache
from .readmodel_client import ReadModelClient
from .readmodel_core import (
    DEFAULT_BOUND_SECONDS,
    LAG_CURRENT,
    LAG_OUTDATED,
    LAG_REBUILD_REQUIRED,
    LAG_STALE,
    PROJECTIONS,
    ConsistencyLevel,
    ProjectionNotFoundError,
    ReadModelError,
    ReadModelStaleError,
    compute_projection_rows,
    drop_projection,
    get_projection_lag,
    get_projection_lag_map,
    publish_projection_build,
    read_projection,
    refresh_projection,
    refresh_projections,
    start_projection_build,
    watermark,
)
from .readmodel_maintainer import ReadModelMaintainer


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
]