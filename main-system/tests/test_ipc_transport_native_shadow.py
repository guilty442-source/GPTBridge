"""section 10.65 act-1 shadow wiring tests for ipc_server transport.

The harness pushes each inbound WebSocket frame through the C
NativeIpcRegistry single-slot transport and compares the echoed payload
item-by-item; Python stays authoritative.  Covers flag parsing, echo
parity, truncation/mismatch evidence, send/recv divergence and
fail-closed disable.  Native-dependent cases skip when the governed
extension is not built.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from ipc.ipc_transport_native_shadow import IpcTransportNativeShadow  # noqa: E402


def _write_policy(root: Path, mode: str) -> None:
    cfg = root / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text(
        json.dumps(
            {
                "schema": "native-shadow-policy/v1",
                "components": {"ipc_server": {"mode": mode}},
            }
        ),
        encoding="utf-8",
    )


def _native_available() -> bool:
    try:
        from core_system.native import _sovereign_native as native

        reg = native.NativeIpcRegistry()
        return hasattr(reg, "transport_send")
    except Exception:
        return False


requires_native = pytest.mark.skipif(
    not _native_available(),
    reason="native pyd unavailable or predates transport bindings",
)


def _records(path: Path) -> list:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


class _StubTransport:
    """Controllable fake of the C transport slot."""

    def __init__(self) -> None:
        self.slot = None
        self.send_ok = True
        self.recv_none = False

    def transport_send(self, payload, seq):
        if not self.send_ok:
            return False
        self.slot = {"payload": payload, "seq": seq}
        return True

    def transport_recv(self):
        if self.recv_none:
            return None
        slot, self.slot = self.slot, None
        return slot


class _RaisingTransport:
    def transport_send(self, *a, **k):
        raise RuntimeError("native boom")

    def transport_recv(self):
        raise RuntimeError("native boom")


# --- policy gate ---


def test_from_policy_absent_without_file(tmp_path: Path) -> None:
    assert IpcTransportNativeShadow.from_policy(tmp_path) is None


def test_from_policy_absent_when_off(tmp_path: Path) -> None:
    _write_policy(tmp_path, "off")
    assert IpcTransportNativeShadow.from_policy(tmp_path) is None


def test_from_policy_refuses_primary(tmp_path: Path) -> None:
    _write_policy(tmp_path, "primary")
    assert IpcTransportNativeShadow.from_policy(tmp_path) is None


def test_from_policy_absent_on_invalid_json(tmp_path: Path) -> None:
    cfg = tmp_path / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text("{ nope", encoding="utf-8")
    assert IpcTransportNativeShadow.from_policy(tmp_path) is None


# --- echo parity / divergence ---


def test_echo_parity_silent(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = IpcTransportNativeShadow(log, _StubTransport())
    shadow.observe_inbound('{"command":"ping"}')
    shadow.observe_inbound(b'{"command":"pong"}')
    assert not log.exists()


def test_send_refusal_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubTransport()
    stub.send_ok = False
    shadow = IpcTransportNativeShadow(log, stub)
    shadow.observe_inbound("msg")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["transport-send"]


def test_recv_empty_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubTransport()
    stub.recv_none = True
    shadow = IpcTransportNativeShadow(log, stub)
    shadow.observe_inbound("msg")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["transport-recv"]


def test_seq_mismatch_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"

    class SeqShift(_StubTransport):
        def transport_recv(self):
            return {"payload": "msg", "seq": 999}

    shadow = IpcTransportNativeShadow(log, SeqShift())
    shadow.observe_inbound("msg")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["transport-seq"]


def test_truncated_payload_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"

    class Truncating(_StubTransport):
        def transport_send(self, payload, seq):
            self.slot = {"payload": payload[:511], "seq": seq}
            return True

    shadow = IpcTransportNativeShadow(log, Truncating())
    shadow.observe_inbound("x" * 600)
    recs = _records(log)
    assert [r["op"] for r in recs] == ["transport-truncated"]


def test_short_mismatch_not_truncated(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"

    class Corrupt(_StubTransport):
        def transport_send(self, payload, seq):
            self.slot = {"payload": payload + "!", "seq": seq}
            return True

    shadow = IpcTransportNativeShadow(log, Corrupt())
    shadow.observe_inbound("short")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["transport-mismatch"]


# --- fail-closed ---


def test_native_error_disables_once(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = IpcTransportNativeShadow(log, _RaisingTransport())
    for _ in range(3):
        shadow.observe_inbound("msg")
    recs = _records(log)
    assert len(recs) == 1
    assert recs[0]["kind"] == "shadow-disabled"
    assert recs[0]["reason"] == "native-transport-error"


# --- real native lockstep ---


@requires_native
def test_real_native_roundtrip(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    shadow = IpcTransportNativeShadow.from_policy(tmp_path)
    assert shadow is not None
    shadow.observe_inbound('{"command":"health","payload":{}}')
    shadow.observe_inbound('{"command":"heartbeat_pong"}')
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "ipc-transport.jsonl"
    )
    assert _records(log) == []


@requires_native
def test_real_native_truncation_evidence(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    shadow = IpcTransportNativeShadow.from_policy(tmp_path)
    shadow.observe_inbound("y" * 700)
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "ipc-transport.jsonl"
    )
    recs = _records(log)
    assert [r["op"] for r in recs] == ["transport-truncated"]
