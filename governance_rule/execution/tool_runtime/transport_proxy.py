"""star-governed-transport-proxy/v1 — P2 stdio sidecar implementation.

Native execution modules (C/C++/C#) never hold token-issuing capability
and never touch the transport store directly.  They speak this JSONL
protocol to a Python transport agent which performs every channel
operation through ``SharedLayerChannel`` — token issuance, route
authorization and the transport store all stay in the Python governance
plane (E4 boundary unchanged).

Wire protocol (one JSON object per line, UTF-8, stdin/stdout):

    {"v":1,"id":"<uuid>","op":"claim","args":{"channel":"system"}}
    -> {"v":1,"id":"<uuid>","ok":true,"result":{...}}
    -> {"v":1,"id":"<uuid>","ok":false,"error":{"code":"...","message":"..."}}

Signatures mirror ``SharedLayerChannel`` exactly: ``claim()`` takes no
arguments (lease/reclaim live in the store), ``request`` uses a
caller-supplied ``request_id`` (generated when absent, aligned with
``GovernedRequestClient``), and ``response()`` is a single non-blocking
consume — the deadline/cancel poll loop lives on the native side
(``request_sync`` parity, testable).

Spec: ``governance_rule/execution/audit/convergence/governed-transport-proxy-v1.md``.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import uuid
from typing import Any, Callable

AGENT_ID = "star-governed-transport-proxy"
PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 2 * 1024 * 1024

_TOOL_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
_VALID_CHANNELS = frozenset({"system", "ai"})
_VALID_MODES = frozenset({"process", "submit"})

# Ops dispatch to either _channel(args, side="process") — claim/respond/
# cancel-poll/notify/stamp/push-ack — or _submit_channel(args) — request/
# response/cancel/push.  Channel mode is enforced at dispatch time.


class ProxyError(Exception):
    """Protocol-level error carrying a closed-set error code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _resolve_dotted(path: str) -> Callable[..., Any]:
    """Resolve ``module:function`` to a callable (route authorizers)."""
    module_name, sep, attr = str(path or "").partition(":")
    if not sep or not module_name or not attr:
        raise ProxyError("BAD_ENVELOPE", "authorizer must be module:function")
    target = getattr(importlib.import_module(module_name), attr, None)
    if not callable(target):
        raise ProxyError("PERMISSION_DENIED", "authorizer not callable")
    return target


def _live_channel_factory() -> Callable[[str, str], Any]:
    """Real ``SharedLayerChannel`` factory (P2 sidecar path).

    The sidecar inherits the governed environment from the native tool
    that spawned it; ``load_authentication`` consumes the bootstrap env
    exactly like ``GovernedToolRuntime``.  Authentication is loaded once
    per tool and shared across its channels (runtime parity).
    """
    cache: dict[str, tuple[Any, Any]] = {}

    def factory(tool_id: str, channel_id: str) -> Any:
        if tool_id not in cache:
            from .governed_runtime import load_authentication, workspace_root

            root = workspace_root(tool_id)
            cache[tool_id] = (root, load_authentication(root))
        root, authentication = cache[tool_id]
        from shared_layer.channel import SharedLayerChannel

        return SharedLayerChannel(root, tool_id, authentication, channel_id)

    return factory


class TransportProxyAgent:
    """Dispatch one JSONL request against the bound governed channels."""

    def __init__(
        self,
        *,
        channel_factory: Callable[[str, str], Any] | None = None,
        authorizer_resolver: Callable[[str], Callable[..., Any]] | None = None,
    ) -> None:
        self._channel_factory = channel_factory or _live_channel_factory()
        self._authorizer_resolver = authorizer_resolver or _resolve_dotted
        self._tool_id: str | None = None
        self._channels: dict[str, Any] = {}
        self._modes: dict[str, str] = {}
        self._submit: dict[str, dict[str, Any]] = {}
        self._bound = False

    # -- envelope ---------------------------------------------------

    def handle_line(self, line: bytes) -> bytes | None:
        """Handle one raw input line; ``None`` = dropped (connection kept)."""
        if len(line) > MAX_LINE_BYTES:
            return None
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(message, dict):
            return None
        response = self.dispatch(message)
        if response is None:
            return None
        return json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"

    def dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("v") != PROTOCOL_VERSION:
            return None
        request_id = message.get("id")
        op = message.get("op")
        if not isinstance(request_id, str) or not request_id or not isinstance(op, str):
            return None
        args = message.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return self._err(request_id, "BAD_ENVELOPE", "args must be an object")
        handler = getattr(self, f"_op_{op}", None)
        if handler is None or op in {"handle_line", "dispatch"}:
            return self._err(request_id, "BAD_ENVELOPE", f"unknown op: {op}")
        try:
            result = handler(args)
        except ProxyError as exc:
            return self._err(request_id, exc.code, str(exc))
        except PermissionError as exc:
            return self._err(request_id, "PERMISSION_DENIED", str(exc)[:200])
        except Exception as exc:  # transport/store failure — summary only
            return self._err(
                request_id, "TRANSPORT_ERROR",
                f"{type(exc).__name__}: {exc}"[:200],
            )
        return {"v": 1, "id": request_id, "ok": True, "result": result}

    @staticmethod
    def _err(request_id: str, code: str, message: str) -> dict[str, Any]:
        return {
            "v": 1,
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }

    # -- helpers ----------------------------------------------------

    def _require_bound(self) -> None:
        if not self._bound:
            raise ProxyError("PERMISSION_DENIED", "hello required first")

    def _str_arg(self, args: dict[str, Any], key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProxyError("BAD_ENVELOPE", f"missing/invalid arg: {key}")
        return value.strip()

    def _channel(self, args: dict[str, Any], *, side: str) -> Any:
        channel_id = args.get("channel")
        if not isinstance(channel_id, str):
            raise ProxyError("BAD_ENVELOPE", "missing arg: channel")
        channel_id = channel_id.strip().casefold()
        channel = self._channels.get(channel_id)
        if channel is None:
            raise ProxyError("CHANNEL_NOT_BOUND", channel_id or "?")
        mode = self._modes[channel_id]
        if mode != side:
            raise ProxyError(
                "CHANNEL_NOT_BOUND", f"{channel_id} is {mode}, op needs {side}"
            )
        return channel

    def _authorize(self, channel_id: str, target: str, command: str) -> None:
        binding = self._submit.get(channel_id)
        if binding is None:
            raise ProxyError("CHANNEL_NOT_BOUND", f"{channel_id} not submit-bound")
        binding["authorizer"](binding["actor"], target, command)

    # -- control ops -------------------------------------------------

    def _op_hello(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._bound:
            raise ProxyError("PERMISSION_DENIED", "already bound")
        tool_id = args.get("tool_id")
        if (
            not isinstance(tool_id, str)
            or _TOOL_ID_RE.fullmatch(tool_id.strip()) is None
            or tool_id.strip() == "main-system"
        ):
            raise ProxyError("PERMISSION_DENIED", "invalid tool_id")
        tool_id = tool_id.strip()
        instance = args.get("workspace_instance_id")
        if not isinstance(instance, str) or not instance:
            raise ProxyError("PERMISSION_DENIED", "workspace_instance_id required")
        channels = args.get("channels")
        if not isinstance(channels, dict) or not channels:
            raise ProxyError("BAD_ENVELOPE", "channels must be a non-empty object")
        submit_spec = args.get("submit") or {}
        if not isinstance(submit_spec, dict):
            raise ProxyError("BAD_ENVELOPE", "submit must be an object")

        modes: dict[str, str] = {}
        submit: dict[str, dict[str, Any]] = {}
        for raw_id, raw_mode in channels.items():
            channel_id = str(raw_id or "").strip().casefold()
            mode = str(raw_mode or "").strip().casefold()
            if channel_id not in _VALID_CHANNELS or mode not in _VALID_MODES:
                raise ProxyError(
                    "PERMISSION_DENIED", f"invalid channel/mode: {raw_id}={raw_mode}"
                )
            modes[channel_id] = mode
            if mode == "submit":
                binding = submit_spec.get(channel_id)
                if not isinstance(binding, dict):
                    raise ProxyError(
                        "PERMISSION_DENIED",
                        f"submit channel {channel_id} requires submit binding",
                    )
                actor = binding.get("actor")
                authorizer = binding.get("authorizer")
                if not isinstance(actor, str) or not actor.strip():
                    raise ProxyError(
                        "PERMISSION_DENIED", f"submit.{channel_id}.actor required"
                    )
                submit[channel_id] = {
                    "actor": actor.strip(),
                    "authorizer": self._authorizer_resolver(authorizer),
                }

        channels_obj = {
            channel_id: self._channel_factory(tool_id, channel_id)
            for channel_id in modes
        }
        self._tool_id = tool_id
        self._channels = channels_obj
        self._modes = modes
        self._submit = submit
        self._bound = True
        return {"agent": AGENT_ID, "v": PROTOCOL_VERSION, "channels": modes}

    def _op_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    # -- process-side ops ---------------------------------------------

    def _op_claim(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_bound()
        return {"request": self._channel(args, side="process").claim()}

    def _op_respond(self, args: dict[str, Any]) -> bool:
        self._require_bound()
        channel = self._channel(args, side="process")
        return bool(
            channel.respond(self._str_arg(args, "request_id"), args.get("response"))
        )

    def _op_request_cancelled(self, args: dict[str, Any]) -> bool:
        self._require_bound()
        channel = self._channel(args, side="process")
        return bool(channel.request_cancelled(self._str_arg(args, "request_id")))

    def _op_progress(self, args: dict[str, Any]) -> bool:
        self._require_bound()
        channel = self._channel(args, side="process")
        return bool(
            channel.progress(
                self._str_arg(args, "request_id"), args.get("payload")
            )
        )

    def _op_notify_for_request(self, args: dict[str, Any]) -> None:
        self._require_bound()
        self._channel(args, side="process").notify_for_request(
            self._str_arg(args, "request_id")
        )
        return None

    def _op_notification_stamp(self, args: dict[str, Any]) -> Any:
        self._require_bound()
        stamp = self._channel(args, side="process").notification_stamp()
        return list(stamp) if isinstance(stamp, tuple) else stamp

    def _op_claim_pushed(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_bound()
        return {"push": self._channel(args, side="process").claim_pushed()}

    def _op_acknowledge_push(self, args: dict[str, Any]) -> bool:
        self._require_bound()
        channel = self._channel(args, side="process")
        push_id = self._str_arg(args, "push_id")
        if "response" in args:
            try:
                return bool(channel.acknowledge_push(push_id, args["response"]))
            except TypeError:
                pass
        return bool(channel.acknowledge_push(push_id))

    # -- submit-side ops ----------------------------------------------

    def _submit_channel(self, args: dict[str, Any]) -> tuple[Any, str]:
        channel_id = str(args.get("channel") or "").strip().casefold()
        channel = self._channels.get(channel_id)
        if channel is None or self._modes.get(channel_id) != "submit":
            raise ProxyError("CHANNEL_NOT_BOUND", channel_id or "?")
        return channel, channel_id

    def _op_request(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_bound()
        channel, channel_id = self._submit_channel(args)
        target = self._str_arg(args, "target_tool_id")
        command = self._str_arg(args, "command")
        payload = args.get("payload")
        if not isinstance(payload, dict):
            raise ProxyError("BAD_ENVELOPE", "payload must be an object")
        self._authorize(channel_id, target, command)
        request_id = args.get("request_id")
        request_id = (
            str(request_id).strip()
            if isinstance(request_id, str) and str(request_id).strip()
            else f"request-{uuid.uuid4().hex}"
        )
        channel.request(
            target, request_id, {**payload, "_governed_command": command}
        )
        return {"request_id": request_id, "queued": True}

    def _op_response(self, args: dict[str, Any]) -> Any:
        self._require_bound()
        channel, _ = self._submit_channel(args)
        return channel.response(
            self._str_arg(args, "target_tool_id"),
            self._str_arg(args, "request_id"),
        )

    def _op_cancel(self, args: dict[str, Any]) -> bool:
        self._require_bound()
        channel, _ = self._submit_channel(args)
        return bool(
            channel.cancel(
                self._str_arg(args, "target_tool_id"),
                self._str_arg(args, "request_id"),
            )
        )

    def _op_push(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_bound()
        channel, channel_id = self._submit_channel(args)
        target = self._str_arg(args, "target_tool_id")
        command = self._str_arg(args, "command")
        payload = args.get("payload")
        if not isinstance(payload, dict):
            raise ProxyError("BAD_ENVELOPE", "payload must be an object")
        self._authorize(channel_id, target, command)
        push_id = args.get("push_id")
        push_id = (
            str(push_id).strip()
            if isinstance(push_id, str) and str(push_id).strip()
            else f"push-{uuid.uuid4().hex}"
        )
        channel.push(target, push_id, {**payload, "_governed_command": command})
        return {"push_id": push_id}


def main() -> int:
    """P2 stdio sidecar entry: stdin JSONL -> stdout JSONL until EOF."""
    agent = TransportProxyAgent()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    try:
        for raw in stdin:
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            out = agent.handle_line(line)
            if out is not None:
                stdout.write(out)
                stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
