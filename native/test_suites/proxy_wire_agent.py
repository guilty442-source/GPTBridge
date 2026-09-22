"""star-governed-transport-proxy/v1 wire interop fixture (M1 mode-B).

Runs the REAL ``TransportProxyAgent`` dispatch loop (envelope validation,
hello binding, channel-mode enforcement, route authorization, closed
error-code mapping) over stdio JSONL — identical framing to
``transport_proxy.main()`` — but with echo-recording fake channels so a
C++ codec driver can be verified end to end without touching the
transport store.

Protocol on stdin/stdout is unchanged.  Every channel invocation is
also emitted as one JSON record on stderr:

    {"call": {"name": "respond", "args": ["req-1", {"ok": true}]}}

Canned behaviours (deterministic):
  * claim()            -> {"request_id":"req-77","command":"diag.run",...}
  * notification_stamp -> (7, 3)
  * claim_pushed       -> {"push_id":"p-1","payload":{"note":"hi"}}
  * respond/progress/acknowledge_push/cancel -> True
  * request_cancelled  -> True
  * request()          -> records payload; response() then returns
                          {"status":"completed","request_id":<rid>,
                           "response":{"echo":<stored payload>}}
                          so callers can verify _governed_command injection
  * authorizer         -> records (actor, target, command); raises
                          PermissionError when command == "forbidden-command"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from governance_rule.execution.tool_runtime.transport_proxy import (  # noqa: E402
    TransportProxyAgent,
)


def _emit_call(name: str, *args) -> None:
    sys.stderr.write(
        json.dumps({"call": {"name": name, "args": list(args)}},
                   ensure_ascii=False, default=str)
        + "\n"
    )
    sys.stderr.flush()


class _EchoChannel:
    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        self._last_request_payload: dict | None = None

    def claim(self):
        _emit_call("claim")
        return {
            "request_id": "req-77",
            "command": "diag.run",
            "payload": {"k": 1},
        }

    def respond(self, request_id, response):
        _emit_call("respond", request_id, response)
        return True

    def request_cancelled(self, request_id):
        _emit_call("request_cancelled", request_id)
        return True

    def progress(self, request_id, payload):
        _emit_call("progress", request_id, payload)
        return True

    def notify_for_request(self, request_id):
        _emit_call("notify_for_request", request_id)

    def notification_stamp(self):
        _emit_call("notification_stamp")
        return (7, 3)

    def claim_pushed(self):
        _emit_call("claim_pushed")
        return {"push_id": "p-1", "payload": {"note": "hi"}}

    def acknowledge_push(self, push_id, response=None):
        _emit_call("acknowledge_push", push_id, response)
        return True

    def request(self, target, request_id, payload):
        _emit_call("request", target, request_id, payload)
        self._last_request_payload = payload

    def response(self, target, request_id):
        _emit_call("response", target, request_id)
        return {
            "status": "completed",
            "request_id": request_id,
            "response": {"echo": self._last_request_payload},
        }

    def cancel(self, target, request_id):
        _emit_call("cancel", target, request_id)
        return True

    def push(self, target, push_id, payload):
        _emit_call("push", target, push_id, payload)


def _factory(tool_id: str, channel_id: str) -> _EchoChannel:
    return _EchoChannel(channel_id)


def _resolver(path: str):
    def authorize(actor: str, target: str, command: str) -> None:
        _emit_call("authorize", actor, target, command)
        if command == "forbidden-command":
            raise PermissionError("PERMISSION_DENIED")

    return authorize


def main() -> int:
    agent = TransportProxyAgent(
        channel_factory=_factory, authorizer_resolver=_resolver
    )
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
