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

File-backed queue mode (opt-in): when ``GPTBRIDGE_WIRE_QUEUE`` names a
directory, request/claim/respond/cancel share state across agent
processes through per-request JSON files (req-/claimed-/done-/
cancelled-<id>.json; claim takes rows via atomic rename). This lets a
tool host spawn *separate* process and submit sidecars and still flow
one logical request end to end. ``GPTBRIDGE_WIRE_REQUESTER_ACTOR``
overrides the requester_actor recorded on queued rows.
"""
from __future__ import annotations

import json
import os
import re
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


_QUEUE_DIR = os.environ.get("GPTBRIDGE_WIRE_QUEUE", "").strip()
_REQUESTER_ACTOR = os.environ.get(
    "GPTBRIDGE_WIRE_REQUESTER_ACTOR", ""
).strip()


def _queue_path(request_id: str, state: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(request_id))
    return Path(_QUEUE_DIR) / f"{state}-{safe}.json"


def _queue_find(request_id: str) -> tuple[Path, str] | None:
    """Locate a request file in any state; returns (path, state)."""
    for state in ("req", "claimed", "done", "cancelled"):
        path = _queue_path(request_id, state)
        if path.exists():
            return path, state
    return None


def _queue_write(path: Path, row: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class _EchoChannel:
    def __init__(self, channel_id: str, tool_id: str = "") -> None:
        self.channel_id = channel_id
        self.tool_id = tool_id
        self._last_request_payload: dict | None = None

    def claim(self):
        _emit_call("claim")
        if _QUEUE_DIR:
            return self._claim_queued()
        return {
            "request_id": "req-77",
            "command": "diag.run",
            "payload": {"k": 1},
        }

    def _claim_queued(self) -> dict | None:
        """File-backed claim: oldest queued row -> claimed (atomic rename)."""
        queue = Path(_QUEUE_DIR)
        try:
            entries = sorted(queue.glob("req-*.json"))
        except OSError:
            return None
        for req_path in entries:
            try:
                row = json.loads(req_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if row.get("target_tool_id") != self.tool_id:
                continue
            claimed_path = _queue_path(row["request_id"], "claimed")
            try:
                os.replace(req_path, claimed_path)  # atomic take
            except OSError:
                continue  # another worker claimed it
            row["status"] = "claimed"
            row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
            _queue_write(claimed_path, row)
            return {
                "request_id": row["request_id"],
                "requester_actor": row["requester_actor"],
                "target_tool_id": row["target_tool_id"],
                "payload": row["payload"],
                "lease_until": row.get("lease_until"),
                "attempt_count": row["attempt_count"],
            }
        return None

    def respond(self, request_id, response):
        _emit_call("respond", request_id, response)
        if _QUEUE_DIR:
            found = _queue_find(request_id)
            if found is None or found[1] != "claimed":
                return False
            path, _ = found
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            row["status"] = "completed"
            row["response"] = response
            _queue_write(_queue_path(request_id, "done"), row)
            try:
                path.unlink()
            except OSError:
                pass
            return True
        return True

    def request_cancelled(self, request_id):
        _emit_call("request_cancelled", request_id)
        if _QUEUE_DIR:
            return _queue_path(request_id, "cancelled").exists()
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
        if _QUEUE_DIR:
            Path(_QUEUE_DIR).mkdir(parents=True, exist_ok=True)
            _queue_write(
                _queue_path(request_id, "req"),
                {
                    "request_id": request_id,
                    "requester_actor": _REQUESTER_ACTOR
                    or f"governance/tool/{self.tool_id}",
                    "target_tool_id": str(target),
                    "payload": payload,
                    "status": "queued",
                    "lease_until": None,
                    "attempt_count": 0,
                },
            )

    def response(self, target, request_id):
        _emit_call("response", target, request_id)
        if _QUEUE_DIR:
            done = _queue_path(request_id, "done")
            if done.exists():
                try:
                    row = json.loads(done.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    row = {}
                return {
                    "status": "completed",
                    "request_id": request_id,
                    "response": row.get("response"),
                }
            return {"status": "pending", "request_id": request_id}
        return {
            "status": "completed",
            "request_id": request_id,
            "response": {"echo": self._last_request_payload},
        }

    def cancel(self, target, request_id):
        _emit_call("cancel", target, request_id)
        if _QUEUE_DIR:
            found = _queue_find(request_id)
            if found is None or found[1] in ("done", "cancelled"):
                return False
            path, state = found
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                row = {"request_id": request_id}
            row["status"] = "cancelled"
            _queue_write(_queue_path(request_id, "cancelled"), row)
            if state == "req":
                try:
                    path.unlink()
                except OSError:
                    pass
            return True
        return True

    def push(self, target, push_id, payload):
        _emit_call("push", target, push_id, payload)


def _factory(tool_id: str, channel_id: str) -> _EchoChannel:
    return _EchoChannel(channel_id, tool_id)


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
