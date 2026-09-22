"""star-governed-transport-proxy/v1 — P2 sidecar dispatcher tests.

Covers the JSONL envelope, hello binding rules, channel-mode enforcement,
route authorization, SharedLayerChannel signature passthrough and the
closed error-code mapping — all against injected fake channels (the live
SharedLayerChannel/store path is covered by the runtime's own suites).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from governance_rule.execution.tool_runtime.transport_proxy import (  # noqa: E402
    TransportProxyAgent,
)


class _FakeChannel:
    """Records calls and returns canned values; stands in for
    SharedLayerChannel without touching the transport store."""

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        self.calls: list[tuple] = []
        self.claim_result = None
        self.respond_result = True
        self.cancelled_result = False
        self.response_state = None
        self.stamp = (7, 3)

    def claim(self):
        self.calls.append(("claim",))
        return self.claim_result

    def respond(self, request_id, response):
        self.calls.append(("respond", request_id, response))
        return self.respond_result

    def request_cancelled(self, request_id):
        self.calls.append(("request_cancelled", request_id))
        return self.cancelled_result

    def progress(self, request_id, payload):
        self.calls.append(("progress", request_id, payload))
        return True

    def notify_for_request(self, request_id):
        self.calls.append(("notify", request_id))

    def notification_stamp(self):
        self.calls.append(("stamp",))
        return self.stamp

    def push(self, target, push_id, payload):
        self.calls.append(("push", target, push_id, payload))

    def claim_pushed(self):
        self.calls.append(("claim_pushed",))
        return {"push_id": "p1", "payload": {"ok": True}}

    def acknowledge_push(self, push_id, response=None):
        self.calls.append(("ack", push_id, response))
        return True

    def request(self, target, request_id, payload):
        self.calls.append(("request", target, request_id, payload))

    def response(self, target, request_id):
        self.calls.append(("response", target, request_id))
        return self.response_state

    def cancel(self, target, request_id):
        self.calls.append(("cancel", target, request_id))
        return True


def _agent() -> tuple[TransportProxyAgent, dict[str, _FakeChannel], list]:
    channels: dict[str, _FakeChannel] = {}
    auth_calls: list[tuple] = []

    def factory(tool_id: str, channel_id: str) -> _FakeChannel:
        ch = _FakeChannel(channel_id)
        channels[channel_id] = ch
        return ch

    def resolver(path: str):
        def authorize(actor: str, target: str, command: str) -> None:
            auth_calls.append((actor, target, command))
            if command == "forbidden-command":
                raise PermissionError("PERMISSION_DENIED")

        return authorize

    return (
        TransportProxyAgent(
            channel_factory=factory, authorizer_resolver=resolver
        ),
        channels,
        auth_calls,
    )


def _hello(agent: TransportProxyAgent, **overrides) -> dict:
    args = {
        "tool_id": "tool-x",
        "workspace_instance_id": "inst-1",
        "channels": {"system": "process", "ai": "submit"},
        "submit": {
            "ai": {"actor": "governance/tool/tool-x",
                   "authorizer": "mod:auth"}
        },
    }
    args.update(overrides)
    return agent.dispatch(
        {"v": 1, "id": "h1", "op": "hello", "args": args}
    )


def _req(op: str, args: dict, rid: str = "r1") -> dict:
    return {"v": 1, "id": rid, "op": op, "args": args}


def test_hello_binds_channels_and_modes():
    agent, channels, _ = _agent()
    resp = _hello(agent)
    assert resp["ok"] is True
    assert resp["result"]["agent"] == "star-governed-transport-proxy"
    assert resp["result"]["channels"] == {
        "system": "process", "ai": "submit"
    }
    assert set(channels) == {"system", "ai"}


def test_hello_rejects_bad_identity():
    agent, _, _ = _agent()
    for tool_id in ("main-system", "Bad_Tool", "", "x" * 65):
        resp = _hello(agent, tool_id=tool_id)
        assert resp["ok"] is False, tool_id
        assert resp["error"]["code"] in {"PERMISSION_DENIED", "BAD_ENVELOPE"}
    assert _hello(agent, workspace_instance_id="")["error"]["code"] == (
        "PERMISSION_DENIED"
    )


def test_hello_requires_authorizer_for_submit_channel():
    agent, _, _ = _agent()
    resp = _hello(agent, submit={})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "PERMISSION_DENIED"


def test_ops_before_hello_denied():
    agent, _, _ = _agent()
    resp = agent.dispatch(_req("claim", {"channel": "system"}))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "PERMISSION_DENIED"


def test_claim_respond_and_cancel_poll_passthrough():
    agent, channels, _ = _agent()
    assert _hello(agent)["ok"]
    sys_ch = channels["system"]

    resp = agent.dispatch(_req("claim", {"channel": "system"}))
    assert resp["result"] == {"request": None}
    sys_ch.claim_result = {"request_id": "r9", "payload": {}}
    resp = agent.dispatch(_req("claim", {"channel": "system"}, "r2"))
    assert resp["result"]["request"]["request_id"] == "r9"

    resp = agent.dispatch(
        _req("respond", {"channel": "system", "request_id": "r9",
                         "response": {"ok": True}})
    )
    assert resp["result"] is True
    assert sys_ch.calls[-1] == ("respond", "r9", {"ok": True})

    resp = agent.dispatch(
        _req("request_cancelled", {"channel": "system", "request_id": "r9"})
    )
    assert resp["result"] is False

    resp = agent.dispatch(
        _req("notification_stamp", {"channel": "system"})
    )
    assert resp["result"] == [7, 3]


def test_channel_mode_enforced():
    agent, _, _ = _agent()
    assert _hello(agent)["ok"]
    # claim on a submit channel -> CHANNEL_NOT_BOUND
    resp = agent.dispatch(_req("claim", {"channel": "ai"}))
    assert resp["error"]["code"] == "CHANNEL_NOT_BOUND"
    # request on a process channel -> CHANNEL_NOT_BOUND
    resp = agent.dispatch(
        _req("request", {"channel": "system", "target_tool_id": "x",
                         "command": "c", "payload": {}})
    )
    assert resp["error"]["code"] == "CHANNEL_NOT_BOUND"
    # undeclared channel -> CHANNEL_NOT_BOUND
    agent2, _, _ = _agent()
    _hello(agent2, channels={"system": "process"})
    resp = agent2.dispatch(_req("claim", {"channel": "ai"}))
    assert resp["error"]["code"] == "CHANNEL_NOT_BOUND"


def test_request_route_authorization_and_command_injection():
    agent, channels, auth_calls = _agent()
    assert _hello(agent)["ok"]
    ai = channels["ai"]

    resp = agent.dispatch(
        _req("request", {"channel": "ai", "target_tool_id": "xingcheng",
                         "command": "do_thing", "payload": {"a": 1}})
    )
    assert resp["ok"] is True
    assert resp["result"]["queued"] is True
    rid = resp["result"]["request_id"]
    assert rid.startswith("request-")
    assert auth_calls == [("governance/tool/tool-x", "xingcheng", "do_thing")]
    assert ai.calls[-1] == (
        "request", "xingcheng", rid,
        {"a": 1, "_governed_command": "do_thing"},
    )


def test_request_denied_route_is_permission_denied():
    agent, _, _ = _agent()
    assert _hello(agent)["ok"]
    resp = agent.dispatch(
        _req("request", {"channel": "ai", "target_tool_id": "evil",
                         "command": "forbidden-command", "payload": {}})
    )
    assert resp["ok"] is False
    assert resp["error"]["code"] == "PERMISSION_DENIED"


def test_response_is_nonblocking_passthrough():
    agent, channels, _ = _agent()
    assert _hello(agent)["ok"]
    ai = channels["ai"]
    resp = agent.dispatch(
        _req("response", {"channel": "ai", "target_tool_id": "xingcheng",
                         "request_id": "r1"})
    )
    assert resp["result"] is None
    ai.response_state = {"status": "completed",
                         "response": {"ok": True, "request_id": "r1"}}
    resp = agent.dispatch(
        _req("response", {"channel": "ai", "target_tool_id": "xingcheng",
                         "request_id": "r1"})
    )
    assert resp["result"]["status"] == "completed"


def test_push_and_ack():
    agent, channels, _ = _agent()
    assert _hello(agent)["ok"]
    resp = agent.dispatch(
        _req("push", {"channel": "ai", "target_tool_id": "xingcheng",
                      "command": "notify", "payload": {"k": 1}})
    )
    assert resp["result"]["push_id"].startswith("push-")
    resp = agent.dispatch(_req("claim_pushed", {"channel": "system"}))
    assert resp["result"]["push"]["push_id"] == "p1"
    resp = agent.dispatch(
        _req("acknowledge_push", {"channel": "system", "push_id": "p1",
                                  "response": {"seen": True}})
    )
    assert resp["result"] is True
    assert channels["system"].calls[-1] == ("ack", "p1", {"seen": True})


def test_transport_error_mapping_and_message_scrub():
    agent, channels, _ = _agent()
    assert _hello(agent)["ok"]

    class _Boom(_FakeChannel):
        def claim(self):
            raise RuntimeError("pg connection lost: /secret/dsn")

    agent._channels["system"] = _Boom("system")
    resp = agent.dispatch(_req("claim", {"channel": "system"}))
    assert resp["ok"] is False
    assert resp["error"]["code"] == "TRANSPORT_ERROR"
    assert "RuntimeError" in resp["error"]["message"]

    class _Denied(_FakeChannel):
        def claim(self):
            raise PermissionError("PERMISSION_DENIED")

    agent._channels["system"] = _Denied("system")
    resp = agent.dispatch(_req("claim", {"channel": "system"}))
    assert resp["error"]["code"] == "PERMISSION_DENIED"


def test_envelope_rules():
    agent, _, _ = _agent()
    # unknown op -> BAD_ENVELOPE with id
    resp = agent.dispatch({"v": 1, "id": "x", "op": "nope", "args": {}})
    assert resp["error"]["code"] == "BAD_ENVELOPE"
    # wrong version / missing id / non-dict -> dropped (None)
    assert agent.dispatch({"v": 2, "id": "x", "op": "ping"}) is None
    assert agent.dispatch({"v": 1, "op": "ping"}) is None
    assert agent.dispatch(["not", "dict"]) is None
    # non-object args -> BAD_ENVELOPE
    resp = agent.dispatch({"v": 1, "id": "x", "op": "ping", "args": [1]})
    assert resp["error"]["code"] == "BAD_ENVELOPE"


def test_handle_line_drops_garbage_and_oversize():
    agent, _, _ = _agent()
    assert agent.handle_line(b"not json\n") is None
    assert agent.handle_line(b"x" * (2 * 1024 * 1024 + 1)) is None
    out = agent.handle_line(
        json.dumps({"v": 1, "id": "p1", "op": "ping", "args": {}}).encode()
    )
    assert out is not None
    assert json.loads(out)["result"] == {"pong": True}


def test_double_hello_denied():
    agent, _, _ = _agent()
    assert _hello(agent)["ok"]
    resp = _hello(agent)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "PERMISSION_DENIED"
