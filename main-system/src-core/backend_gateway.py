"""Stable loopback gateway for atomic A/B backend generation handover."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_BUFFER_SIZE: Final[int] = 64 * 1024
# bounded-concurrency/v1 (A116): declared envelope for concurrent
# bridged connections; the effective cap is the governor "network" class
# quota clamped into [_CONN_MIN, _CONN_MAX], fail-open to _CONN_MAX.
_CONN_MIN: Final[int] = 8
_CONN_MAX: Final[int] = 64
_GOVERNOR_STATE: Final[Path] = (
    Path(__file__).resolve().parents[1]
    / "runtime" / "state" / "resource-governor.json"
)


@dataclass(frozen=True)
class BackendTarget:
    port: int
    generation: str


class BackendGateway:
    """TCP gateway whose public listener survives backend replacement.

    Each accepted connection is pinned to the active generation. Switching the
    target is atomic for new connections; existing WebSocket/HTTP connections
    drain against the old generation and may be closed after a bounded grace.
    """

    def __init__(
        self, public_port: int, host: str = "127.0.0.1", *,
        max_connections: int | None = None,
    ) -> None:
        self.host = host
        self.public_port = public_port
        self._target: BackendTarget | None = None
        self._target_lock = threading.Lock()
        self._connections: dict[int, set[socket.socket]] = {}
        self._connections_lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        # bounded-concurrency/v1: capacity = governor network quota,
        # refreshed lazily (≤5 s stale) so a dead governor never stalls
        # the accept loop.
        self._max_connections = max_connections
        self._quota_checked = 0.0

    @property
    def active_target(self) -> BackendTarget | None:
        with self._target_lock:
            return self._target

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(
            thread is not None
            and thread.is_alive()
            and self._listener is not None
            and not self._stop.is_set()
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # On Windows SO_REUSEADDR lets a second process bind the same
            # address, producing duplicate gateways that split frontend
            # traffic.  Prefer SO_EXCLUSIVEADDRUSE so a competing supervisor
            # fails fast and an existing gateway is replaced only through the
            # governed kill-and-rebind path.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                )
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.public_port))
        except OSError:
            listener.close()
            raise
        listener.listen(128)
        listener.settimeout(0.5)
        self._listener = listener
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, name="backend-gateway", daemon=True
        )
        self._thread.start()

    def activate(self, port: int, generation: str) -> BackendTarget | None:
        target = BackendTarget(port=port, generation=generation)
        with self._target_lock:
            previous = self._target
            self._target = target
        return previous

    def connection_count(self, port: int) -> int:
        with self._connections_lock:
            return len(self._connections.get(port, ()))

    def close_generation_connections(self, port: int) -> None:
        with self._connections_lock:
            sockets = list(self._connections.get(port, ()))
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._connections_lock:
            ports = list(self._connections)
        for port in ports:
            self.close_generation_connections(port)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            target = self.active_target
            if target is None:
                client.close()
                continue
            # bounded-concurrency/v1 (capacity + reject): a full gateway
            # closes the connection instead of growing the bridge fleet.
            with self._connections_lock:
                active = sum(len(s) for s in self._connections.values()) // 2
            if active >= self._connection_cap():
                client.close()
                continue
            threading.Thread(
                target=self._bridge,
                args=(client, target),
                name=f"backend-gateway-{target.generation}",
                daemon=True,
            ).start()

    def _connection_cap(self) -> int:
        if self._max_connections is not None:
            return max(1, self._max_connections)
        now = time.monotonic()
        if now - self._quota_checked < 5.0:
            return _CONN_MAX
        self._quota_checked = now
        try:
            from shared_layer.adaptive.budget_source import class_quota
        except ImportError:
            return _CONN_MAX
        quota = class_quota("network", _GOVERNOR_STATE)
        if quota is None:
            return _CONN_MAX
        return max(_CONN_MIN, min(_CONN_MAX, quota.quota * 8))

    def _bridge(self, client: socket.socket, target: BackendTarget) -> None:
        upstream: socket.socket | None = None
        try:
            upstream = socket.create_connection(
                (self.host, target.port), timeout=3.0
            )
            client.settimeout(None)
            upstream.settimeout(None)
            with self._connections_lock:
                self._connections.setdefault(target.port, set()).update(
                    (client, upstream)
                )
            left = threading.Thread(
                target=self._copy, args=(client, upstream), daemon=True
            )
            right = threading.Thread(
                target=self._copy, args=(upstream, client), daemon=True
            )
            left.start()
            right.start()
            left.join()
            right.join()
        except OSError:
            pass
        finally:
            for connection in (client, upstream):
                if connection is None:
                    continue
                with self._connections_lock:
                    self._connections.get(target.port, set()).discard(connection)
                try:
                    connection.close()
                except OSError:
                    pass

    @staticmethod
    def _copy(source: socket.socket, target: socket.socket) -> None:
        try:
            while True:
                data = source.recv(_BUFFER_SIZE)
                if not data:
                    break
                target.sendall(data)
        except OSError:
            pass
        finally:
            # Half-close propagation: signal EOF downstream so the peer
            # closes its side, which unblocks the sibling copy direction.
            # Without this a one-sided close can leave the bridge (and its
            # threads) parked in recv forever.  Only the write side of the
            # *target* is shut — a pending response in the reverse direction
            # is still allowed to complete.
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass


__all__ = ["BackendGateway", "BackendTarget"]
