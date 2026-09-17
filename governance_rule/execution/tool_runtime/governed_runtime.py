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
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Final
from urllib.parse import parse_qs, urlsplit

import websockets  # type: ignore

from governance_rule.execution.authentication import (
    GovernanceAuthenticationService,
    LauncherIdentityAttestation,
)
from governance_rule.execution.integrity import AuthorityIntegrityManifest
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)

from shared_layer.channel import SharedLayerChannel

from .sub_sovereign import (
    SUB_SOVEREIGN_AUTHORITY,
    SUB_SOVEREIGN_DUTY,
    SUB_SOVEREIGN_ROLE,
    SUB_SOVEREIGN_SCOPE,
    SUB_SOVEREIGN_UNDER,
    ChannelHealth,
)

from .governed_runtime_worker import GovernedRuntimeWorkerMixin
from .governed_runtime_maintenance import GovernedRuntimeMaintenanceMixin


BOOTSTRAP_ENV: Final[str] = "GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP"
TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-f0-9]{64}$")
Executor = Callable[[str, dict[str, Any], str], Awaitable[tuple[str, dict[str, Any]]]]
Lifecycle = Callable[[], Awaitable[Any]]
Cancellation = Callable[[str], Awaitable[bool]]
MAX_OUTPUT_CHARACTERS: Final[int] = 2 * 1024 * 1024
LOCAL_CLEANUP_COMMAND: Final[str] = "toolbox_run_local_cleanup"
GOVERNANCE_MAIN_ACTOR: Final[str] = "governance/main-system"


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def permission_denied() -> PermissionError:
    return PermissionError("PERMISSION_DENIED")


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_SUB_SOVEREIGN_MEMBERS: Final[tuple[str, ...]] = (
    "tool_id",
    "sovereign_id",
    "version",
    "channels",
    "channel_for",
    "health_snapshot",
    "run",
    "role",
)

# class-level members（methods/property）可在模組載入時靜態檢查；其實例
# 屬性成員（tool_id/sovereign_id/version）由 __init__ 末尾之相符斷言保證。
_SUB_SOVEREIGN_CLASS_MEMBERS: Final[tuple[str, ...]] = (
    "channels",
    "channel_for",
    "health_snapshot",
    "run",
    "role",
)


def _assert_sub_sovereign(runtime: Any) -> None:
    missing = [
        member for member in _SUB_SOVEREIGN_MEMBERS if not hasattr(runtime, member)
    ]
    if missing:
        raise AssertionError(
            f"NOT_SUB_SOVEREIGN: {runtime.__class__.__name__} missing {missing}"
        )


def _sealed_identity_root_match(
    tool_id: str, root: Path, tool_root: Path, manifest_path: Path
) -> bool:
    """Whether a sealed-registry identity may run from this tool root.

    An identity runs either inside its own registered bound root (the
    independent tool's canonical directory, at any nesting depth) or from a
    physical host directory that contains the identity's bound manifest —
    the nested-owner case such as ``local-model`` hosting ``xingcheng``.
    A tool_id that is not a sealed identity is never accepted here.
    """
    for identity in identity_group_snapshot().identities:
        if str(identity.bound_tool_id or "").strip() != tool_id:
            continue
        for bound_root in identity.bound_roots or ():
            bound = (
                root / str(bound_root).format(tool_id=tool_id)
            ).resolve()
            try:
                tool_root.relative_to(bound)
                return True
            except ValueError:
                continue
        binding = identity.manifest_binding
        if binding is not None and binding.required and binding.path_template:
            bound_manifest = (
                root / binding.path_template.format(tool_id=tool_id)
            ).resolve()
            if bound_manifest == manifest_path.resolve():
                return True
            try:
                bound_manifest.relative_to(tool_root)
            except ValueError:
                return False
            return bound_manifest.is_file()
        return False
    return False


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
    if root != Path("E:/GPTBridge").resolve():
        raise permission_denied()
    if _sealed_identity_root_match(tool_id, root, tool_root, manifest_path):
        return root
    if len(relative.parts) in {1, 2} and manifest.get("id") == tool_id:
        return root
    raise permission_denied()


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


class GovernedToolRuntime(GovernedRuntimeWorkerMixin, GovernedRuntimeMaintenanceMixin):
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
        sovereign_id: str = SUB_SOVEREIGN_UNDER[0],
    ) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", tool_id) is None:
            raise permission_denied()
        self.tool_id = tool_id
        self.version = version
        self.sovereign_id = sovereign_id
        self.root = workspace_root(tool_id)
        self.tool_root = Path(
            os.environ.get("GPTBRIDGE_TOOL_DIR") or self.root / tool_id
        ).resolve()
        self.authentication = load_authentication(self.root)
        self._init_channels(channel_modes)
        self._init_token_and_port()
        self._init_callbacks(startup, shutdown, cancellation, executor, health, idle_cleanup)
        self._init_state(self_repair, self_repair_clear_pycache, local_cleanup)
        _assert_sub_sovereign(self)
        self._start_time = time.monotonic()

    def _init_channels(self, channel_modes: dict[str, str] | None) -> None:
        """Initialize shared-layer channels (system + optional AI)."""
        self.channel = SharedLayerChannel(
            self.root,
            self.tool_id,
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
                self.tool_id,
                self.authentication,
                channel_id,
            )
            if mode == "process":
                self._processing_channel_ids.append(channel_id)

    def _init_token_and_port(self) -> None:
        """Initialize and validate IPC token and port from environment."""
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

    def _init_callbacks(
        self,
        startup: Lifecycle | None,
        shutdown: Lifecycle | None,
        cancellation: Cancellation | None,
        executor: Executor,
        health: Callable[[], dict[str, Any]] | None,
        idle_cleanup: Callable[[], Any] | None,
    ) -> None:
        """Assign lifecycle callbacks."""
        self.executor = executor
        self.startup_callback = startup
        self.shutdown_callback = shutdown
        self.cancellation = cancellation
        self.health_callback = health
        self.idle_cleanup = idle_cleanup

    def _init_state(
        self,
        self_repair: bool,
        self_repair_clear_pycache: bool,
        local_cleanup: bool,
    ) -> None:
        """Initialize mutable runtime state."""
        self.waiters: dict[str, Any] = {}
        self.self_repair_enabled = self_repair
        self.self_repair_clear_pycache = self_repair_clear_pycache
        self._last_self_repair: dict[str, Any] | None = None
        self.local_cleanup_enabled = local_cleanup
        self._last_local_cleanup: dict[str, Any] | None = None
        self._channel_health: dict[str, ChannelHealth] = {
            channel_id: ChannelHealth(channel_id=channel_id)
            for channel_id in self._channels
        }
        # Notification channel state (best-effort wake-up acceleration).
        self._last_notification: dict[str, Any] | None = None
        self._notified_request_ids: set[str] = set()
        self._start_time = time.monotonic()

    def channel_for(self, channel_id: str) -> SharedLayerChannel:
        channel = self._channels.get(str(channel_id or "").strip().casefold())
        if channel is None:
            raise permission_denied()
        return channel

    def channels(self) -> tuple[str, ...]:
        return tuple(sorted(self._channels))

    @property
    def role(self) -> str:
        return SUB_SOVEREIGN_ROLE

    def _record_channel_health(self, channel_id: str, ok: bool) -> None:
        current = self._channel_health.setdefault(
            channel_id, ChannelHealth(channel_id=channel_id)
        )
        consecutive = 0 if ok else current.consecutive_failures + 1
        self._channel_health[channel_id] = ChannelHealth(
            channel_id=channel_id,
            last_ok=ok,
            last_request_at=_iso_now(),
            consecutive_failures=consecutive,
            degraded=consecutive >= 3,
        )

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
        """Forward transport change signals into the worker's wake queue.

        The central PostgreSQL transport announces queued requests with
        ``pg_notify('tool_request_<channel>')``; the local degraded
        transport has no LISTEN source, so this listener watches the local
        store's write stamp (a single cheap ``stat`` per processing
        channel) and enqueues a wake token whenever the database or its
        WAL sidecar changes.  The worker therefore claims immediately on a
        new request instead of waiting out its idle backoff, and every
        delivered token is handled by ``_on_channel_notification``.
        """
        last_stamp: dict[str, tuple[int, int] | None] = {}
        while not self.shutdown_event.is_set():
            changed = False
            for channel_id in self._processing_channel_ids:
                channel = self._channels.get(channel_id)
                probe = getattr(channel, "notification_stamp", None)
                if not callable(probe):
                    continue
                try:
                    stamp = await asyncio.to_thread(probe)
                except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
                    stamp = None
                if stamp is None:
                    continue
                if last_stamp.get(channel_id) != stamp:
                    last_stamp[channel_id] = stamp
                    changed = True
            if changed:
                with contextlib.suppress(asyncio.QueueFull):
                    notify_queue.put_nowait("transport-store-changed")
            await asyncio.sleep(0.05 if changed else 0.25)

    def _on_channel_notification(self, payload: str) -> None:
        """Handle one delivered transport notification token.

        Records the notification for health/observability, lets the worker
        clear its idle backoff (it re-claims from the store immediately
        after a notification), and remembers an embedded ``request_id``
        when the payload is a structured JSON event so health surfaces can
        correlate which request triggered the wake-up.  Malformed payloads
        are ignored — the notification channel is best-effort acceleration
        on top of the authoritative store claim.
        """
        self._last_notification = {
            "payload": str(payload)[:200],
            "received_at": _iso_now(),
        }
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(decoded, dict):
            return
        request_id = str(decoded.get("request_id") or "").strip()
        if request_id:
            self._notified_request_ids.add(request_id)
            if len(self._notified_request_ids) > 128:
                self._notified_request_ids.clear()


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


# 靜態相符保證：GovernedToolRuntime 必須滿足統一子主宰契約（SubSovereign）。
if not all(
    hasattr(GovernedToolRuntime, member)
    for member in _SUB_SOVEREIGN_CLASS_MEMBERS
):
    raise NotImplementedError(
        "GovernedToolRuntime does not satisfy the sub-sovereign contract"
    )
