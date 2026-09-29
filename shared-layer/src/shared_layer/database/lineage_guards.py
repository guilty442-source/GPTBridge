"""Lineage guards: best-effort context + provenance declaration."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from psycopg import Connection

from shared_layer.database.provenance import clear_provenance, set_provenance

_logger = logging.getLogger("gptbridge.lineage")


@contextmanager
def best_effort(*log_kwargs_keys: str) -> Iterator[None]:
    """Context guard for lineage calls that must never break the write path.

    Usage:
        with lineage_best_effort("module_id", "resource_id"):
            record_rag_resource(conn, ...)

    Any exception raised inside is logged (via ``_logger.warning``) and
    swallowed so a lineage failure never blocks the governed write or the
    outbox pipeline. Values for the logged keys are taken from kwargs passed
    by the caller through ``**kwargs``; if no kwargs are supplied the guard
    simply logs the exception message.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — lineage is best-effort
        _logger.warning("lineage best-effort record failed: %s", exc)


@contextmanager
def lineage_provenance(
    connection: Connection[Any],
    *,
    actor_id: Optional[str] = None,
    executor_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    generation: Optional[int] = None,
    source_revision: Optional[int] = None,
) -> Iterator[None]:
    """Declare provenance on a connection for the duration of the write.

    Wraps :func:`provenance.set_provenance` and clears the variables on exit
    so subsequent unrelated statements in the same session stay clean.
    """
    set_provenance(
        connection,
        actor_id=actor_id,
        executor_id=executor_id,
        decision_id=decision_id,
        correlation_id=correlation_id,
        generation=generation,
        source_revision=source_revision,
    )
    try:
        yield
    finally:
        clear_provenance(connection)
