from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Final
from urllib.parse import parse_qs, urlsplit

import websockets  # type: ignore

from governance_rule.execution.authentication import (
    GovernanceAuthenticationService,
    LauncherIdentityAttestation,
)
from governance_rule.execution.integrity import AuthorityIntegrityManifest

from .channel import SharedLayerChannel


BOOTSTRAP_ENV: Final[str] = "GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP"
TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-f0-9]{64}$")
Executor = Callable[[str, dict[str, Any], str], Awaitable[tuple[str, dict[str, Any]]]]
Lifecycle = Callable[[], Awaitable[Any]]
Cancellation = Callable[[str], Awaitable[bool]]
MAX_OUTPUT_CHARACTERS: Final[int] = 2 * 1024 * 1024


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def permission_denied() -> PermissionError:
    return PermissionError("PERMISSION_DENIED")


def workspace_root(tool_id: str) -> Path:
    raw = str(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT") or "").strip()
    root = Path(raw).resolve() if raw else Path("E:/GPTBridge").resolve()
    tool_raw = str(os.environ.get("GPTBRIDGE_TOOL_DIR") or "").strip()
    tool_root = Path(tool_raw).resolve() if tool_raw else (root / tool_id).resolve()
    try:
        relative = tool_root.relative_to(root)
    except ValueError:
        raise permission_denied()
    manifest_path = tool_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise permission_denied()
    if (
        root != Path("E:/GPTBridge").resolve()
        or len(relative.parts) not in {1, 2}
        or manifest.get("id") != tool_id
    ):
        raise permission_denied()
    return root


def load_authentication(root: Path) -> GovernanceAuthenticationService:
    encoded = os.environ.pop(BOOTSTRAP_ENV, None)
    if not encoded:
        raise permission_denied()
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("format_version") != 1:
            raise permission_denied()
        launcher_key = base64.b64decode(payload["launcher_key"], validate=True)
        integrity_raw = payload["integrity_manifest"]
        attestation_raw = payload["identity_attestation"]
        integrity = AuthorityIntegrityManifest(
            authority_version=integrity_raw["authority_version"],
            file_digests=tuple(
                (str(row[0]), str(row[1])) for row in integrity_raw["file_digests"]
            ),
            issued_at=integrity_raw["issued_at"],
            key_id=integrity_raw["key_id"],
            signature=integrity_raw["signature"],
        )
        attestation = LauncherIdentityAttestation(**attestation_raw)
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise permission_denied() from exc
    return GovernanceAuthenticationService(root, launcher_key, integrity, attestation)


def _http_response(status: int, reason: str, body: bytes, content_type: str):
    try:
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        return Response(
            status,
            reason,
            Headers(
                [("Content-Type", content_type), ("Content-Length", str(len(body)))],
            ),
            body,
        )
    except ImportError:
        import http

        return (
            http.HTTPStatus(status),
            [("Content-Type", content_type), ("Content-Length", str(len(body)))],
            body,
        )


class GovernedToolRuntime:
    def __init__(
        self,
        *,
        tool_id: str,
        version: str,
        executor: Executor,
        startup: Lifecycle | None = None,
        shutdown: Lifecycle | None = None,
        cancellation: Cancellation | None = None,
        health: Callable[[], dict[str, Any]] | None = None,
        idle_cleanup: Callable[[], Any] | None = None,
        channel_modes: dict[str, str] | None = None,
        self_repair: bool = True,
        self_repair_clear_pycache: bool = True,
        local_cleanup: bool = True,
    ) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", tool_id) is None:
            raise permission_denied()
        self.tool_id = tool_id
        self.version = version
        self.root = workspace_root(tool_id)
        self.tool_root = Path(
            os.environ.get("GPTBRIDGE_TOOL_DIR") or self.root / tool_id
        ).resolve()
        self.authentication = load_authentication(self.root)
        self.channel = SharedLayerChannel(
            self.root,
            tool_id,
            self.authentication,
            "system",
        )
        self._channels: dict[str, SharedLayerChannel] = {"system": self.channel}
        self._processing_channel_ids: list[str] = ["system"]
        for raw_channel_id, raw_mode in dict(channel_modes or {}).items():
            channel_id = str(raw_channel_id or "").strip().casefold()
            mode = str(raw_mode or "").strip().casefold()
            if channel_id != "ai" or mode not in {"submit", "process"}:
                raise permission_denied()
            self._channels[channel_id] = SharedLayerChannel(
                self.root,
                tool_id,
                self.authentication,
                channel_id,
            )
            if mode == "process":
                self._processing_channel_ids.append(channel_id)
        token = str(os.environ.get("GPTBRIDGE_IPC_SESSION_TOKEN") or "").strip().lower()
        if TOKEN_PATTERN.fullmatch(token) is None:
            raise permission_denied()
        self.token = token
        try:
            self.port = int(str(os.environ.get("GPTBRIDGE_IPC_PORT") or ""))
        except ValueError as exc:
            raise permission_denied() from exc
        if not 1024 <= self.port <= 65535:
            raise permission_denied()
        self.shutdown_token = str(os.environ.get("GPTBRIDGE_SHUTDOWN_TOKEN") or "")
        self.shutdown_event = asyncio.Event()
        self.executor = executor
        self.startup_callback = startup
        self.shutdown_callback = shutdown
        self.cancellation = cancellation
        self.health_callback = health
        self.waiters: dict[str, Any] = {}
        self.idle_cleanup = idle_cleanup
        self.self_repair_enabled = self_repair
        self.self_repair_clear_pycache = self_repair_clear_pycache
        self._last_self_repair: dict[str, Any] | None = None
        self.local_cleanup_enabled = local_cleanup
        self._last_local_cleanup: dict[str, Any] | None = None

    def channel_for(self, channel_id: str) -> SharedLayerChannel:
        channel = self._channels.get(str(channel_id or "").strip().casefold())
        if channel is None:
            raise permission_denied()
        return channel

    def workspace_instance_id(self) -> str:
        normalized = os.path.normcase(str(self.root)).replace("\\", "/")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    async def send(self, websocket: Any, event: str, payload: dict[str, Any]) -> None:
        await websocket.send(json.dumps({"event": event, "payload": payload}, ensure_ascii=False))

    async def emit(self, request_id: str, event: str, payload: dict[str, Any]) -> None:
        websocket = self.waiters.get(request_id)
        if websocket is not None:
            with contextlib.suppress(Exception):
                await self.send(websocket, event, payload)

    async def _listen_for_notifications(self, notify_queue: asyncio.Queue[str]) -> None:
        # Codex-native local transport has no PostgreSQL LISTEN source. The
        # worker polls the local sqlite store directly; this task stays for
        # interface parity and simply waits out the runtime.
        while not self.shutdown_event.is_set():
            await asyncio.sleep(0.5)

    def _on_channel_notification(self, payload: str) -> None:
        pass

    async def _worker(self) -> None:
        idle_poll_seconds = 0.25
        notify_queue: asyncio.Queue[str] = asyncio.Queue()
        listener_task: asyncio.Task[Any] | None = None
        try:
            listener_task = asyncio.create_task(
                self._listen_for_notifications(notify_queue)
            )
        except Exception:
            listener_task = None
        while not self.shutdown_event.is_set():
            request = None
            request_channel: SharedLayerChannel | None = None
            try:
                for channel_id in self._processing_channel_ids:
                    candidate_channel = self._channels[channel_id]
                    candidate = await asyncio.to_thread(candidate_channel.claim)
                    if candidate is not None:
                        request = candidate
                        request_channel = candidate_channel
                        break
            except Exception:
                # Temporary SQLite/WAL contention must not permanently stop
                # the governed worker while its health endpoint stays online.
                await asyncio.sleep(0.5)
                continue
            if request is None:
                wait_timeout = (
                    0.05
                    if not notify_queue.empty()
                    else max(idle_poll_seconds, 0.05)
                )
                try:
                    notification = await asyncio.wait_for(
                        notify_queue.get(), timeout=wait_timeout
                    )
                    self._on_channel_notification(notification)
                    idle_poll_seconds = 0.25
                    continue
                except asyncio.TimeoutError:
                    idle_poll_seconds = min(idle_poll_seconds * 1.5, 0.5)
                    continue
            idle_poll_seconds = 0.25
            request_id = str(request["request_id"])
            payload = request.get("payload")
            try:
                if not isinstance(payload, dict):
                    raise permission_denied()
                command = str(payload.pop("_governed_command", "")).strip()
                if not command:
                    raise permission_denied()
                payload["_governed_requester_actor"] = str(
                    request.get("requester_actor") or ""
                )
                execution_task = asyncio.create_task(
                    self.executor(command, payload, request_id)
                )
                cancelled_during_execution = False
                while not execution_task.done():
                    done, _ = await asyncio.wait({execution_task}, timeout=0.1)
                    if done:
                        break
                    if request_channel is not None and await asyncio.to_thread(
                        request_channel.request_cancelled, request_id
                    ):
                        cancelled_during_execution = True
                        if self.cancellation is not None:
                            await self.cancellation(request_id)
                        execution_task.cancel()
                        await asyncio.gather(execution_task, return_exceptions=True)
                        self.waiters.pop(request_id, None)
                        break
                if cancelled_during_execution:
                    continue
                event, result = execution_task.result()
                if not isinstance(result, dict):
                    raise permission_denied()
                # The transport request id is authoritative. Business payloads
                # may carry nested ids, but the websocket client can only
                # correlate the response with the id registered in waiters.
                result["request_id"] = request_id
            except Exception:
                event = "error"
                result = {
                    "ok": False,
                    "tool_id": self.tool_id,
                    "request_id": request_id,
                    "error_code": "PERMISSION_DENIED",
                    "message": "PERMISSION_DENIED",
                }
            try:
                if request_channel is None:
                    raise permission_denied()
                await asyncio.to_thread(request_channel.respond, request_id, result)
            except Exception:
                event = "error"
                result = {
                    "ok": False,
                    "tool_id": self.tool_id,
                    "request_id": request_id,
                    "error_code": "PERMISSION_DENIED",
                    "message": "PERMISSION_DENIED",
                }
            websocket = self.waiters.pop(request_id, None)
            if websocket is not None:
                with contextlib.suppress(Exception):
                    await self.send(websocket, event, result)

    async def _handler(self, websocket: Any) -> None:
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
                command = str(message.get("command") or "").strip()
                payload = message.get("payload")
                if not command or not isinstance(payload, dict):
                    raise permission_denied()
                request_id = str(payload.get("request_id") or "").strip()
                if command == "toolbox_cancel_tool_run":
                    cancelled = bool(request_id) and await asyncio.to_thread(
                        self.channel.cancel, self.tool_id, request_id
                    )
                    if self.cancellation is not None and request_id:
                        cancelled = await self.cancellation(request_id) or cancelled
                    await self.send(
                        websocket,
                        "toolbox_cancel_tool_run_result",
                        {
                            "ok": cancelled,
                            "cancelled": cancelled,
                            "tool_id": self.tool_id,
                            "request_id": request_id,
                        },
                    )
                    continue
                if not request_id or len(request_id) > 256:
                    raise permission_denied()
                if str(payload.get("tool_id") or self.tool_id) != self.tool_id:
                    raise permission_denied()
                queued_payload = dict(payload)
                queued_payload["_governed_command"] = command
                self.waiters[request_id] = websocket
                await asyncio.to_thread(
                    self.channel.request,
                    self.tool_id,
                    request_id,
                    queued_payload,
                )
                await self.send(
                    websocket,
                    "COMMAND_RECEIVED",
                    {"command": command, "status": "processing"},
                )
            except Exception:
                await self.send(
                    websocket,
                    "error",
                    {"error_code": "PERMISSION_DENIED", "message": "PERMISSION_DENIED"},
                )

    async def _run_local_self_repair(self) -> dict[str, Any]:
        from .tool_self_repair import run_local_self_repair

        try:
            result = await asyncio.to_thread(
                run_local_self_repair,
                self.tool_id,
                self.tool_root,
                clear_pycache=self.self_repair_clear_pycache,
            )
        except Exception as error:  # never block boot on self repair
            result = {
                "ok": False,
                "operation": "local-self-repair",
                "authority": "tool-local",
                "tool_id": self.tool_id,
                "database_errors": [str(error)],
                "errors": [str(error)],
            }
        self._last_self_repair = result
        return result

    def _self_repair_health(self) -> dict[str, Any]:
        last = self._last_self_repair or {}
        return {
            "self_repair": {
                "enabled": self.self_repair_enabled,
                "completed": self._last_self_repair is not None,
                "last_ok": last.get("ok"),
                "checked_databases": last.get("checked_databases") or [],
                "preserved_databases": last.get("preserved_databases") or [],
                "database_errors": last.get("database_errors") or [],
            }
        }

    async def _run_local_cleanup(self) -> dict[str, Any]:
        from .tool_local_cleanup import run_local_cleanup

        try:
            result = await asyncio.to_thread(
                run_local_cleanup,
                self.tool_id,
                self.tool_root,
            )
        except Exception as error:  # never block boot on local cleanup
            result = {
                "ok": False,
                "operation": "local-self-cleanup",
                "authority": "tool-local",
                "tool_id": self.tool_id,
                "cleaned_files": [],
                "cleaned_directories": [],
                "skipped": [{"reason": f"{type(error).__name__}: {error}"}],
                "cleaned_bytes": 0,
            }
        self._last_local_cleanup = result
        return result

    def _local_cleanup_health(self) -> dict[str, Any]:
        last = self._last_local_cleanup or {}
        return {
            "local_cleanup": {
                "enabled": self.local_cleanup_enabled,
                "completed": self._last_local_cleanup is not None,
                "last_ok": last.get("ok"),
                "cleaned_files": last.get("cleaned_files") or [],
                "cleaned_directories": last.get("cleaned_directories") or [],
                "cleaned_bytes": last.get("cleaned_bytes") or 0,
            }
        }

    async def run(self) -> None:
        worker: asyncio.Task[Any] | None = None

        def process_request(_connection: Any, request: Any):
            request_path = urlsplit(str(request.path))
            if request_path.path == "/health":
                extra = self.health_callback() if self.health_callback else {}
                body = json.dumps(
                    {
                        "ok": True,
                        "version": self.version,
                        "tool_id": self.tool_id,
                        "runtime_scope": "independent-tool",
                        "governance_ready": True,
                        "workspace_instance_id": self.workspace_instance_id(),
                        "channels": sorted(self._channels),
                        "channel_routes": {
                            channel_id: f"{channel_id}-channel/{self.tool_id}"
                            for channel_id in self._channels
                        },
                        "_self_repair": self._self_repair_health()
                        if self.self_repair_enabled
                        else {},
                        "_local_cleanup": self._local_cleanup_health()
                        if self.local_cleanup_enabled
                        else {},
                        **extra,
                    }
                ).encode("utf-8")
                return _http_response(200, "OK", body, "application/json")
            if request_path.path == "/shutdown":
                supplied = str(request.headers.get("X-GPTBridge-Shutdown-Token") or "")
                if not self.shutdown_token or not hmac.compare_digest(
                    supplied, self.shutdown_token
                ):
                    return _http_response(403, "FORBIDDEN", b"Forbidden", "text/plain")
                self.shutdown_event.set()
                return _http_response(200, "OK", b"OK", "text/plain")
            query = parse_qs(request_path.query)
            supplied_token = str((query.get("token") or [""])[0]).lower()
            supplied_instance = str((query.get("instance") or [""])[0])
            if (
                not hmac.compare_digest(supplied_token, self.token)
                or supplied_instance != self.workspace_instance_id()
            ):
                return _http_response(403, "FORBIDDEN", b"Forbidden", "text/plain")
            return None

        try:
            if self.self_repair_enabled:
                await self._run_local_self_repair()
            if self.local_cleanup_enabled:
                await self._run_local_cleanup()
            if self.startup_callback is not None:
                await self.startup_callback()
            # The owner service must finish initializing before an existing
            # governed request can claim its business locks. Starting the
            # queue worker earlier races startup recovery against the first
            # request and can deadlock an otherwise healthy tool.
            worker = asyncio.create_task(self._worker())
            async with websockets.serve(
                self._handler,
                "127.0.0.1",
                self.port,
                origins=(None, "file://", "null"),
                process_request=process_request,
            ):
                await self.shutdown_event.wait()
        finally:
            if self.shutdown_callback is not None:
                await self.shutdown_callback()
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if self.idle_cleanup is not None:
                await asyncio.to_thread(self.idle_cleanup)
            self.authentication.close()


class GovernedCliExecutor:
    def __init__(self, tool_id: str, tool_root: Path | str) -> None:
        self.tool_id = tool_id
        self.tool_root = Path(tool_root).resolve()
        self.entry = (self.tool_root / "src" / "main.py").resolve()
        if self.entry.parent.parent != self.tool_root or not self.entry.is_file():
            raise permission_denied()
        self.processes: dict[str, asyncio.subprocess.Process] = {}

    async def __call__(
        self,
        command: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> tuple[str, dict[str, Any]]:
        if command not in {"toolbox_run_tool", "toolbox_request_tool_execution"}:
            raise permission_denied()
        raw_args = payload.get("args", [])
        if (
            not isinstance(raw_args, list)
            or len(raw_args) > 256
            or not all(isinstance(item, str) and "\x00" not in item for item in raw_args)
        ):
            raise permission_denied()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {BOOTSTRAP_ENV, "GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP"}
        }
        environment.update(
            {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        requester_actor = str(
            payload.get("_governed_requester_actor") or ""
        ).strip()
        if requester_actor:
            environment["GPTBRIDGE_GOVERNED_REQUESTER_ACTOR"] = requester_actor
        environment["GPTBRIDGE_GOVERNED_REQUEST_ID"] = request_id
        process = await asyncio.create_subprocess_exec(
            os.fspath(Path(os.sys.executable).resolve()),
            "-B",
            "-s",
            os.fspath(self.entry),
            *raw_args,
            cwd=os.fspath(self.tool_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            **_background_subprocess_kwargs(),
        )
        self.processes[request_id] = process
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=1800
            )
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
        finally:
            self.processes.pop(request_id, None)
        stdout = stdout_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARACTERS]
        stderr = stderr_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARACTERS]
        result = {
            "ok": process.returncode == 0 and not timed_out,
            "tool_id": self.tool_id,
            "request_id": request_id,
            "status": "timed_out" if timed_out else (
                "completed" if process.returncode == 0 else "failed"
            ),
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_encoding": "utf-8",
            "stderr_encoding": "utf-8",
            "timed_out": timed_out,
            "cancelled": False,
            "message": "Tool completed" if process.returncode == 0 else "Tool failed",
        }
        return f"{command}_result", result

    async def cancel(self, request_id: str) -> bool:
        process = self.processes.get(request_id)
        if process is None or process.returncode is not None:
            return False
        process.kill()
        return True
