"""star-governed-transport-proxy/v1 wire interop (M1 mode-B).

End-to-end verification that the C++ codec (native/tool_runtime/
transport_proxy_client.cpp, driven via bin/proxy_client_driver.exe)
and the REAL Python TransportProxyAgent speak the same wire protocol:

    C++ args builder -> C++ encode_request -> agent stdin
    agent stdout -> C++ decode_response_line -> assertions

Channel invocations inside the agent are recorded on its stderr so the
test can verify argument pass-through and _governed_command injection.
RequestWaiter completion semantics are exercised against a real agent
response state.  Skips when the driver binary has not been built.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "native" / "test_suites" / "bin" / "proxy_client_driver.exe"
AGENT = ROOT / "native" / "test_suites" / "proxy_wire_agent.py"

pytestmark = pytest.mark.skipif(
    not DRIVER.is_file(), reason="proxy_client_driver.exe not built"
)


def driver(*args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(
        [str(DRIVER), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, f"driver {args[0]} failed: {proc.stderr}"
    return proc.stdout.strip()


def encode(rid: str, op: str, *builder_argv: str) -> str:
    args_json = driver(*builder_argv) if builder_argv else "{}"
    return driver("encode", rid, op, args_json)


def decode(line: str) -> dict:
    out = driver("decode", stdin=line + "\n")
    return json.loads(out)


class AgentProc:
    """One proxy_wire_agent subprocess; records live on stderr."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.calls: list[dict] = []

    def send(self, line: str) -> dict:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(line.encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        assert raw, "agent closed stdout unexpectedly"
        return decode(raw.decode("utf-8").rstrip("\r\n"))

    def close(self) -> list[dict]:
        assert self.proc.stdin and self.proc.stderr
        self.proc.stdin.close()
        self.proc.wait(timeout=30)
        self.calls = [
            json.loads(raw)["call"]
            for raw in self.proc.stderr.read().decode("utf-8").splitlines()
            if raw.strip()
        ]
        return self.calls


@pytest.fixture()
def agent():
    proc = AgentProc()
    yield proc
    if proc.proc.poll() is None:
        proc.close()


def hello(agent: AgentProc) -> dict:
    line = encode(
        "h-1",
        "hello",
        "args-hello",
        "tool-x",
        "inst-1",
        "system=process,ai=submit",
        "ai|governance/tool/tool-x|mod:auth",
    )
    return agent.send(line)


def test_hello_binds_channels(agent: AgentProc):
    res = hello(agent)
    assert res["ok"] is True
    assert res["id"] == "h-1"
    result = res["result"]
    assert result["agent"] == "star-governed-transport-proxy"
    assert result["v"] == 1
    assert result["channels"] == {"system": "process", "ai": "submit"}


def test_ping(agent: AgentProc):
    res = agent.send(encode("p-1", "ping", "args-empty"))
    assert res["ok"] is True and res["result"] == {"pong": True}


def test_process_channel_ops(agent: AgentProc):
    hello(agent)

    res = agent.send(encode("c-1", "claim", "args-channel", "system"))
    assert res["ok"] is True
    assert res["result"]["request"]["request_id"] == "req-77"

    res = agent.send(
        encode("r-1", "respond", "args-respond", "system", "req-77",
               '{"ok":true}')
    )
    assert res["ok"] is True and res["result"] is True

    res = agent.send(
        encode("x-1", "request_cancelled", "args-request-id", "system",
               "req-77")
    )
    assert res["ok"] is True and res["result"] is True

    res = agent.send(
        encode("g-1", "progress", "args-progress", "system", "req-77",
               '{"pct":50}')
    )
    assert res["ok"] is True and res["result"] is True

    res = agent.send(
        encode("n-1", "notify_for_request", "args-request-id", "system",
               "req-77")
    )
    assert res["ok"] is True and res["result"] is None

    res = agent.send(
        encode("s-1", "notification_stamp", "args-channel", "system")
    )
    assert res["ok"] is True and res["result"] == [7, 3]

    res = agent.send(
        encode("cp-1", "claim_pushed", "args-channel", "system")
    )
    assert res["ok"] is True
    assert res["result"]["push"]["push_id"] == "p-1"

    res = agent.send(
        encode("a-1", "acknowledge_push", "args-ack", "system", "p-1",
               '{"done":true}')
    )
    assert res["ok"] is True and res["result"] is True

    calls = {c["name"]: c["args"] for c in agent.close()}
    assert calls["respond"] == ["req-77", {"ok": True}]
    assert calls["progress"] == ["req-77", {"pct": 50}]
    assert calls["notify_for_request"] == ["req-77"]
    assert calls["acknowledge_push"] == ["p-1", {"done": True}]


def test_submit_channel_request_response_waiter(agent: AgentProc):
    hello(agent)

    res = agent.send(
        encode(
            "q-1",
            "request",
            "args-request",
            "ai",
            "xingcheng",
            "diag.run",
            '{"k":2}',
            "req-9",
        )
    )
    assert res["ok"] is True
    assert res["result"] == {"request_id": "req-9", "queued": True}

    res = agent.send(
        encode("q-2", "response", "args-submit-response", "ai",
               "xingcheng", "req-9")
    )
    assert res["ok"] is True
    state = res["result"]
    assert state["status"] == "completed"

    # Feed the real agent state into the C++ RequestWaiter.
    w = json.loads(driver("waiter", "100.0", json.dumps(state)))
    assert w["state"] == "Completed"
    # request_id stripped by RequestWaiter; echo carries the submitted
    # payload which must contain the injected _governed_command.
    echo = w["result"]["echo"]
    assert "request_id" not in w["result"]
    assert echo["_governed_command"] == "diag.run"
    assert echo["k"] == 2
    assert w["deadline_exceeded"] is True  # now = deadline + 60

    res = agent.send(
        encode("q-3", "cancel", "args-submit-response", "ai",
               "xingcheng", "req-9")
    )
    assert res["ok"] is True and res["result"] is True

    res = agent.send(
        encode("q-4", "push", "args-push", "ai", "xingcheng",
               "diag.push", '{"m":1}', "push-9")
    )
    assert res["ok"] is True and res["result"] == {"push_id": "push-9"}

    calls = agent.close()
    request_call = next(c for c in calls if c["name"] == "request")
    assert request_call["args"][0] == "xingcheng"
    assert request_call["args"][1] == "req-9"
    payload = request_call["args"][2]
    assert payload["_governed_command"] == "diag.run"
    assert payload["k"] == 2


def test_errors(agent: AgentProc):
    hello(agent)

    # submit-side op against process channel -> CHANNEL_NOT_BOUND
    res = agent.send(
        encode("e-1", "request", "args-request", "system", "xingcheng",
               "diag.run", '{}', "-")
    )
    assert res["ok"] is False and res["error_code"] == "CHANNEL_NOT_BOUND"

    # process-side op against submit channel -> CHANNEL_NOT_BOUND
    res = agent.send(encode("e-2", "claim", "args-channel", "ai"))
    assert res["ok"] is False and res["error_code"] == "CHANNEL_NOT_BOUND"

    # unknown op -> BAD_ENVELOPE
    res = agent.send(encode("e-3", "bogus_op", "args-empty"))
    assert res["ok"] is False and res["error_code"] == "BAD_ENVELOPE"

    # authorizer denial -> PERMISSION_DENIED
    res = agent.send(
        encode("e-4", "request", "args-request", "ai", "xingcheng",
               "forbidden-command", '{}', "-")
    )
    assert res["ok"] is False and res["error_code"] == "PERMISSION_DENIED"
    agent.close()


def test_claim_before_hello_denied():
    proc = AgentProc()
    res = proc.send(encode("u-1", "claim", "args-channel", "system"))
    assert res["ok"] is False and res["error_code"] == "PERMISSION_DENIED"
    proc.close()


def test_decode_drops_garbage():
    out = driver("decode", stdin="not-json\n[1,2]\n")
    lines = [json.loads(raw) for raw in out.splitlines() if raw.strip()]
    assert lines == [{"valid": False}, {"valid": False}]


class SidecarDriver:
    """proxy_client_driver.exe sidecar — spawns the real agent itself."""

    def __init__(self, *command: str) -> None:
        quoted = " ".join(f'"{part}"' for part in command)
        self.proc = subprocess.Popen(
            [str(DRIVER), "sidecar", quoted],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def send(self, op: str, *builder_argv: str) -> dict:
        args_json = driver(*builder_argv) if builder_argv else "{}"
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(
            (op + "\t" + args_json + "\n").encode("utf-8")
        )
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        assert raw, "driver closed stdout unexpectedly"
        return json.loads(raw.decode("utf-8"))

    def close(self) -> int:
        assert self.proc.stdin
        self.proc.stdin.close()
        return self.proc.wait(timeout=30)


@pytest.fixture()
def sidecar():
    drv = SidecarDriver(sys.executable, str(AGENT))
    yield drv
    if drv.proc.poll() is None:
        drv.close()


def test_sidecar_spawn_and_process_ops(sidecar: SidecarDriver):
    res = sidecar.send(
        "hello",
        "args-hello",
        "tool-x",
        "inst-1",
        "system=process,ai=submit",
        "ai|governance/tool/tool-x|mod:auth",
    )
    assert res["ok"] is True
    assert res["result"]["agent"] == "star-governed-transport-proxy"

    res = sidecar.send("ping", "args-empty")
    assert res["ok"] is True and res["result"] == {"pong": True}

    res = sidecar.send("claim", "args-channel", "system")
    assert res["ok"] is True
    assert res["result"]["request"]["request_id"] == "req-77"

    res = sidecar.send(
        "respond", "args-respond", "system", "req-77", '{"ok":true}'
    )
    assert res["ok"] is True and res["result"] is True

    assert sidecar.close() == 0


def test_sidecar_submit_and_waiter(sidecar: SidecarDriver):
    sidecar.send(
        "hello",
        "args-hello",
        "tool-x",
        "inst-1",
        "system=process,ai=submit",
        "ai|governance/tool/tool-x|mod:auth",
    )
    res = sidecar.send(
        "request", "args-request", "ai", "xingcheng", "diag.run",
        '{"k":5}', "req-42"
    )
    assert res["ok"] is True
    assert res["result"] == {"request_id": "req-42", "queued": True}

    res = sidecar.send(
        "response", "args-submit-response", "ai", "xingcheng", "req-42"
    )
    assert res["ok"] is True
    w = json.loads(driver("waiter", "100.0", json.dumps(res["result"])))
    assert w["state"] == "Completed"
    assert w["result"]["echo"]["_governed_command"] == "diag.run"
    sidecar.close()


def test_sidecar_protocol_error_passthrough(sidecar: SidecarDriver):
    res = sidecar.send("claim", "args-channel", "system")
    assert res["ok"] is False and res["error_code"] == "PERMISSION_DENIED"
    sidecar.close()


def test_sidecar_spawn_failure():
    drv = SidecarDriver("definitely-not-a-real-exe-xyz123.exe")
    assert drv.proc.stdout is not None
    first = json.loads(drv.proc.stdout.readline().decode("utf-8"))
    assert first["transport_error"] == "PROXY_SPAWN_FAILED"
    assert drv.proc.wait(timeout=30) != 0
