"""E2EHarness — wires real services for the benchmark paths.

- HTTP: real loopback ``http.server`` on 127.0.0.1:0 (request echo).
- SQL: real sqlite3 temp-file database (data-IO representative; the
  ``engine`` metadata field keeps this honest — it is NOT PostgreSQL).
- Native: shared ``NativeExecutionRuntime`` + the real
  ``_sovereign_native`` pyd ``vector_dot``; unavailable → path marked.
- C# adapter: real process spawn (see stages.csharp_adapter).
- Model wait: injected callable only; never faked.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .paths import PathContext


class _EchoHandler(BaseHTTPRequestHandler):
    """Minimal loopback echo — stands in for the ipcSession HTTP leg."""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
            ok = isinstance(payload, dict) and "payload" in payload
        except Exception:
            ok = False
        response = json.dumps({"ok": ok}).encode("utf-8")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args: Any) -> None:
        pass  # no logging on the measured path


class E2EHarness:
    """Owns real services for a benchmark run; context-managed."""

    def __init__(self, *, native_workers: int = 1,
                 model_call: Optional[Any] = None) -> None:
        self._http: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._tmpdir: Optional[tempfile.TemporaryDirectory[str]] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._native_runtime: Any = None
        self._native_dot: Optional[Any] = None
        self._native_workers = native_workers
        self._model_call = model_call
        self.native_available = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "E2EHarness":
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        self._http_thread = threading.Thread(
            target=self._http.serve_forever, daemon=True, name="e2e-http"
        )
        self._http_thread.start()

        self._tmpdir = tempfile.TemporaryDirectory(prefix="e2e-perf-")
        db_path = Path(self._tmpdir.name) / "perf.sqlite3"
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE perf_kv (key TEXT PRIMARY KEY, value TEXT)"
        )
        self._conn.executemany(
            "INSERT INTO perf_kv (key, value) VALUES (?, ?)",
            [(f"k{i}", "v" * 256) for i in range(64)],
        )
        self._conn.commit()

        try:
            from core_system.native.execution_runtime import (
                NativeExecutionRuntime,
            )
            import core_system.native._sovereign_native as native

            self._native_runtime = NativeExecutionRuntime(
                max_workers=self._native_workers, queue_limit=64
            )
            self._native_dot = native.vector_dot
            self.native_available = True
        except Exception:
            self._native_runtime = None
            self._native_dot = None
        return self

    def services(self, *, model_call: Optional[Any] = None) -> PathContext:
        assert self._http is not None and self._conn is not None
        port = self._http.server_address[1]
        size = 2560
        return PathContext(
            http_endpoint=f"http://127.0.0.1:{port}/ipc",
            sql_conn=self._conn,
            sql_engine="sqlite-tempfile",
            native_runtime=self._native_runtime,
            native_dot=self._native_dot,
            native_vectors=[[0.5] * size, [0.25] * size],
            model_call=model_call if model_call is not None else self._model_call,
        )

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        if self._native_runtime is not None:
            self._native_runtime.shutdown(wait=True)
            self._native_runtime = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    def __enter__(self) -> "E2EHarness":
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.stop()


__all__ = ["E2EHarness"]
