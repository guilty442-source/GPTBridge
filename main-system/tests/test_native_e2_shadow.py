"""E2 native shadow bindings smoke tests (§10.65).

Same contract as test_native_e1_shadow: verified on a fresh
``_sovereign_native`` build; skips while the running backend holds the
locked .pyd on an older revision.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
sys.path.insert(0, str(SRC_CORE))

try:
    from core_system.native import _sovereign_native as _native

    _E2 = hasattr(_native, "NativeIpcRegistry")
except ImportError:
    _E2 = False

requires_e2 = pytest.mark.skipif(
    not _E2, reason="native pyd predates E2 bindings (rebuild pending)"
)


@requires_e2
def test_registry_create_find_and_status_flow():
    reg = _native.NativeIpcRegistry()
    assert reg.count() == 0
    assert reg.create("req-1", 42)
    assert not reg.create("req-1", 42)  # duplicate rejected
    assert reg.count() == 1
    row = reg.find("req-1")
    assert row["status"] == "CREATED"
    assert row["backend_generation"] == 42
    assert reg.set_status("req-1", 1)  # QUEUED
    assert reg.set_status("req-1", 2)  # RUNNING
    assert reg.find("req-1")["status"] == "RUNNING"
    assert reg.find("missing") is None


@requires_e2
def test_registry_terminal_state_not_reentrant():
    reg = _native.NativeIpcRegistry()
    reg.create("req-2", 1)
    reg.set_status("req-2", 2)  # RUNNING
    reg.set_status("req-2", 3)  # COMPLETED
    # terminal → different status must fail; same status is idempotent
    assert not reg.set_status("req-2", 2)
    assert reg.set_status("req-2", 3)


@requires_e2
def test_registry_cancel_idempotent_and_interrupted_resume():
    reg = _native.NativeIpcRegistry()
    reg.create("req-3", 1)
    reg.set_status("req-3", 2)  # RUNNING
    assert reg.cancel("req-3")
    assert reg.find("req-3")["status"] == "CANCELLED"
    assert reg.cancel("req-3")  # idempotent
    # INTERRUPTED is resumable (§10.23): INTERRUPTED → RUNNING legal
    reg.create("req-4", 1)
    reg.set_status("req-4", 2)
    reg.set_status("req-4", 7)  # INTERRUPTED
    assert reg.set_status("req-4", 2)
    assert reg.find("req-4")["status"] == "RUNNING"


@requires_e2
def test_transport_single_slot_roundtrip():
    reg = _native.NativeIpcRegistry()
    assert reg.transport_recv() is None
    assert reg.transport_send("hello", 7)
    msg = reg.transport_recv()
    assert msg["payload"] == "hello"
    assert msg["seq"] == 7
    assert reg.transport_recv() is None
