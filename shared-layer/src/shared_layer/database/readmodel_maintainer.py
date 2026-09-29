"""ReadModelMaintainer — optional periodic projection refresh loop."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from psycopg import Connection

from .readmodel_core import PROJECTIONS, refresh_projections

_logger = logging.getLogger("gptbridge.readmodel")


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
