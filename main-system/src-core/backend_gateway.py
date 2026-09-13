"""Stable loopback gateway for atomic A/B backend generation handover."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Final

_BUFFER_SIZE: Final[int] = 64 * 1024


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

    def __init__(self, public_port: int, host: str = "127.0.0.1") -> None:
        self.host = host
        self.public_port = public_port
        self._target: BackendTarget | None = None
        self._target_lock = threading.Lock()
        self._connections: dict[int, set[socket.socket]] = {}
        self._connections_lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

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
            threading.Thread(
                target=self._bridge,
                args=(client, target),
                name=f"backend-gateway-{target.generation}",
                daemon=True,
            ).start()

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
