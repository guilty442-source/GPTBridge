"""星澄模型服務 HTTP 端點（``star-model-service/v1``；G30）。

Loopback-only HTTP surface that lets the C# business-orchestration layer
reach the governed inference paths (``generate_via_native_engine`` and,
through its flag, the C++ runtime).  The service never opens a non-loopback
socket and never exposes weights — only the versioned request/response
contract.

Endpoints:

- ``POST /v1/infer``   — governed generation (maps to ``generate_via_native_engine``)
- ``GET  /v1/status``  — control-plane status (engine flags, checkpoint)
- ``POST /v1/release`` — explicit auto-release of cached engines

Authentication reuses the standalone session token
(``runtime/ipc/session-token``) via the ``X-GPTBridge-Session-Token``
header — the same trust boundary as the tool's other IPC surfaces.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCHEMA = "star-model-service/v1"
DESCRIPTOR_SCHEMA = "star-model-service-descriptor/v1"
MAX_BODY_BYTES = 64 * 1024
MAX_INFLIGHT = 2
SESSION_HEADER = "X-GPTBridge-Session-Token"


def tool_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _descriptor_path(root: Path) -> Path:
    return root / "xingcheng" / "runtime" / "ipc" / "model-service.json"


def _token_path(root: Path) -> Path:
    return root / "xingcheng" / "runtime" / "ipc" / "model-service-session-token"


def _session_token(root: Path) -> str:
    """Load the standalone session token; empty when none exists."""
    candidates = [
        root / "xingcheng" / "runtime" / "ipc" / "session-token",
        root / "runtime" / "ipc" / "session-token",
    ]
    for candidate in candidates:
        try:
            token = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            return token
    return ""


class _Handler(BaseHTTPRequestHandler):
    server: "ModelServiceHTTPServer"  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # keep stdout clean; errors are surfaced via response codes

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _deny(self, status: int, code: str, message: str) -> None:
        self._send_json(
            status,
            {
                "schema": SCHEMA,
                "ok": False,
                "error_code": code,
                "message": message,
            },
        )

    def _authorized(self) -> bool:
        expected = self.server.session_token
        if not expected:
            return False  # fail-closed: no token material → no service
        provided = self.headers.get(SESSION_HEADER) or ""
        return hmac.compare_digest(provided, expected)

    def _read_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._deny(413, "MODEL_SERVICE_BODY_TOO_LARGE", "request body exceeds 64KiB")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._deny(400, "MODEL_SERVICE_BODY_INVALID", "request body is not valid JSON")
            return None
        if not isinstance(payload, dict):
            self._deny(400, "MODEL_SERVICE_BODY_INVALID", "request body must be a JSON object")
            return None
        return payload

    def _bounded(self, fn) -> None:
        """Serialize expensive work through a bounded inflight gate."""
        if not self.server.inflight.acquire(timeout=10.0):
            self._deny(503, "MODEL_SERVICE_BUSY", "inflight request limit reached")
            return
        try:
            fn()
        finally:
            self.server.inflight.release()

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._deny(403, "MODEL_SERVICE_UNAUTHORIZED", "session token required")
            return
        if self.path == "/v1/status":
            self._handle_status()
        else:
            self._deny(404, "MODEL_SERVICE_ROUTE_UNKNOWN", "unknown route")

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._deny(403, "MODEL_SERVICE_UNAUTHORIZED", "session token required")
            return
        if self.path == "/v1/infer":
            self._bounded(lambda: self._handle_infer())
        elif self.path == "/v1/release":
            self._handle_release()
        else:
            self._deny(404, "MODEL_SERVICE_ROUTE_UNKNOWN", "unknown route")

    def _handle_status(self) -> None:
        from .native_engine import control_status
        from .native_transformer.cpp_runtime import cpp_runtime_mode

        status = control_status()
        self._send_json(
            200,
            {
                "schema": SCHEMA,
                "ok": True,
                "pid": os.getpid(),
                "cpp_runtime": cpp_runtime_mode(),
                "engine": status,
            },
        )

    def _handle_infer(self) -> None:
        payload = self._read_body()
        if payload is None:
            return
        prompt = str(payload.get("prompt") or "")
        prompts = payload.get("prompts")
        batch_prompts = (
            [str(p or "") for p in prompts]
            if isinstance(prompts, list) and prompts
            else []
        )
        if not prompt.strip() and not batch_prompts:
            self._deny(400, "PROMPT_REQUIRED", "prompt must not be empty")
            return
        extra = payload.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        seed = payload.get("seed", extra.get("seed"))
        request: dict[str, Any] = {
            "prompt": prompt,
            "intent": str(payload.get("intent") or ""),
            "max_tokens": payload.get("max_new_tokens"),
            "temperature": payload.get("temperature"),
            "top_k": payload.get("top_k"),
            "top_p": payload.get("top_p"),
            "seed": seed,
        }
        if batch_prompts:
            request["prompts"] = batch_prompts
        started = time.perf_counter()
        from .native_engine import generate_via_native_engine

        result = generate_via_native_engine(request)
        latency_ms = round((time.perf_counter() - started) * 1_000, 3)
        ok = bool(result.get("ok"))
        if batch_prompts and "results" in result:
            results = result.get("results") or []
            response: dict[str, Any] = {
                "schema": SCHEMA,
                "ok": ok,
                "batch_size": int(result.get("batch_size") or len(results)),
                "results": [
                    {
                        "ok": bool(r.get("ok")),
                        "text": str(r.get("text") or ""),
                        "token_ids": list(r.get("token_ids") or []),
                        "model_id": str(r.get("model") or ""),
                        "decoder": str(r.get("decoder") or ""),
                        "cpp_runtime": bool(r.get("cpp_runtime")),
                    }
                    for r in results
                ],
                "latency_ms": float(result.get("latency_ms") or latency_ms),
            }
            if not ok:
                response["error_code"] = str(
                    result.get("error_code") or "INFERENCE_FAILED"
                )
                response["message"] = str(result.get("message") or "")
            self._send_json(200 if ok else 502, response)
            return
        response = {
            "schema": SCHEMA,
            "ok": ok,
            "text": str(result.get("text") or ""),
            "token_ids": list(result.get("token_ids") or []),
            "model_id": str(result.get("model") or ""),
            "latency_ms": float(result.get("latency_ms") or latency_ms),
            "decoder": str(result.get("decoder") or ""),
            "cpp_runtime": bool(result.get("cpp_runtime")),
        }
        if not ok:
            response["error_code"] = str(result.get("error_code") or "INFERENCE_FAILED")
            response["message"] = str(result.get("message") or "")
        self._send_json(200 if ok else 502, response)

    def _handle_release(self) -> None:
        body = self._read_body()
        if body is None:
            return
        from .native_transformer.execution.auto_release import get_manager

        manager = get_manager()
        key = str(body.get("key") or "")
        with manager._lock:  # noqa: SLF001 - same-process lifecycle control
            keys = [key] if key else list(manager._resources.keys())  # noqa: SLF001
        released = [k for k in keys if manager.release(k)]
        self._send_json(
            200,
            {"schema": SCHEMA, "ok": True, "released": released},
        )


class ModelServiceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, session_token: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.session_token = session_token
        self.inflight = threading.BoundedSemaphore(MAX_INFLIGHT)


class ModelService:
    """Lifecycle wrapper: start → write descriptor → serve → stop."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or tool_root()
        self._server: ModelServiceHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1]) if self._server else 0

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> dict[str, Any]:
        if self._server is not None:
            return {"ok": True, "port": self.port, "already_running": True}
        token = _session_token(self._root)
        if not token:
            # Per-boot token when the standalone IPC token does not exist yet
            # (e.g. tests, pre-channel boot).
            token = hashlib.sha256(
                f"{os.getpid()}|{time.time_ns()}".encode("utf-8")
            ).hexdigest()
        server = ModelServiceHTTPServer(token)
        descriptor = _descriptor_path(self._root)
        descriptor.parent.mkdir(parents=True, exist_ok=True)
        # Governed consumers (C# orchestration layer) read the token from this
        # file — same local-user trust boundary as the standalone IPC token.
        token_file = _token_path(self._root)
        token_file.write_text(token, encoding="utf-8")
        try:
            os.chmod(token_file, 0o600)
        except OSError:
            pass
        payload = {
            "schema": DESCRIPTOR_SCHEMA,
            "tool_id": "local-model",
            "pid": os.getpid(),
            "port": server.server_address[1],
            "token_file": token_file.name,
            "lifecycle_owner": "local-model/channel_runtime.py",
            "consumer_policy": "csharp-orchestrator-client-only",
            "session_token_sha256": hashlib.sha256(
                token.encode("utf-8")
            ).hexdigest(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = descriptor.with_name(descriptor.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, descriptor)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="xingcheng-model-service-http",
            daemon=True,
        )
        self._thread.start()
        return {"ok": True, "port": self.port, "descriptor": str(descriptor)}

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        for path in (_descriptor_path(self._root), _token_path(self._root)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "DESCRIPTOR_SCHEMA",
    "MAX_BODY_BYTES",
    "MAX_INFLIGHT",
    "ModelService",
    "SCHEMA",
    "SESSION_HEADER",
]
