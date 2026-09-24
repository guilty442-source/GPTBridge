"""M1 investment-mobile C# shadow parity tests (module-language-migration).

The C# prototype in ``native/test_suites/csharp_investment`` mirrors the
decision-free execution semantics of the investment-mobile Python tool
(channel_runtime / integration+infrastructure clients / use_cases /
presenters). ``InvestmentMobileShadow.exe`` emits a fixed case matrix as
JSON; this test replays the same matrix through the authoritative Python
functions and compares outputs field-by-field.

Skips when the dotnet toolchain or a built exe is unavailable.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRV = (
    PROJECT_ROOT
    / "Standalone tools"
    / "investment-mobile"
    / "src"
    / "backend"
    / "services"
)
EXE = (
    PROJECT_ROOT
    / "native"
    / "test_suites"
    / "csharp_investment"
    / "bin"
    / "Release"
    / "net10.0"
    / "InvestmentMobileShadow.exe"
)
sys.path.insert(0, str(SRV))
sys.path.insert(0, str(PROJECT_ROOT))

requires_exe = pytest.mark.skipif(
    not EXE.is_file(),
    reason="InvestmentMobileShadow.exe not built (dotnet build pending)",
)

# The tool's channel_runtime.py shares its module name with other tools'
# runtimes (e.g. file-sorter, which conftest puts on sys.path) — load it
# explicitly by path. Module-level code requires the governance root env.
import importlib.util  # noqa: E402
import os  # noqa: E402

os.environ.setdefault(
    "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", str(PROJECT_ROOT)
)
_CR_SPEC = importlib.util.spec_from_file_location(
    "investment_mobile_channel_runtime",
    PROJECT_ROOT
    / "Standalone tools"
    / "investment-mobile"
    / "src"
    / "channel_runtime.py",
)
cr = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(cr)  # type: ignore[union-attr]  # noqa: E402
from investment_mobile.integration.clients import (  # noqa: E402
    ChannelClient,
    INSTRUCTION_COMMAND,
    SNAPSHOT_COMMAND,
)
# NOTE: the use-cases / infrastructure-clients / presenters parity cases are
# superseded — the old business surface was removed in the 星澄 AI 投資管理與
# 自動操盤系統 rebuild; the C# shadow exe mirrors the retired semantics.



def _csharp_cases() -> dict[str, dict]:
    proc = subprocess.run(
        [str(EXE)],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    cases = json.loads(proc.stdout.decode("utf-8"))
    return {case["id"]: case["output"] for case in cases}


@pytest.fixture(scope="module")
def csharp() -> dict[str, dict]:
    return _csharp_cases()


class EchoStub:
    """Mirror of the C# EchoChannelStub: echoes payload + command."""

    connected = True

    async def request(self, target, command, payload, timeout_seconds=None):
        return {"ok": True, "echo": payload, "command": command}


class FailingStub:
    connected = True

    async def request(self, target, command, payload, timeout_seconds=None):
        return {"ok": False, "error_code": "CHANNEL_REQUEST_FAILED"}


def _bound_channel(stub) -> ChannelClient:
    client = ChannelClient(cr.TOOL_ID)
    client._client = stub
    return client


@requires_exe
def test_owns_and_requester_parity(csharp):
    for case_id, output in csharp.items():
        if case_id.startswith("owns:"):
            assert output == cr.service.owns(case_id.split(":", 1)[1])
        elif case_id.startswith("requester:"):
            actor = case_id.split(":", 1)[1]
            actor = None if actor == "<null>" else actor
            assert output == (actor in cr._AUTHORIZED_REQUESTERS)


@requires_exe
def test_execute_gate_parity(csharp):
    def py_gate(requester, command):
        allowed = requester in cr._AUTHORIZED_REQUESTERS
        return "ALLOW" if allowed and cr.service.owns(command) \
            else "PERMISSION_DENIED"

    cases = {
        "execute_gate:authorized+owned": (
            "governance/main-system", "investment-mobile-status"),
        "execute_gate:authorized+foreign": (
            "governance/main-system", "bogus"),
        "execute_gate:unauthorized+owned": (
            "governance/tool/other", "investment-mobile-status"),
    }
    for case_id, (requester, command) in cases.items():
        assert csharp[case_id] == py_gate(requester, command)


@requires_exe
@pytest.mark.asyncio
async def test_handle_routing_parity(csharp):
    payload = {"request_id": "r-1", "key": "v"}
    cr.service._started = True
    cr.service.channel = _bound_channel(EchoStub())
    commands = [
        "investment-mobile-status", "investment-mobile-start",
        "investment-analysis", "investment-manager", "bogus",
    ]
    for cmd in commands:
        case = csharp[f"handle:{cmd}"]
        try:
            event, result = await cr.service.handle(cmd, dict(payload))
        except PermissionError:
            event, result = "PERMISSION_DENIED", {"ok": False}
        assert case["event"] == event
        # The rebuilt service may add fields to status results; the C#
        # matrix predates them — compare on the emitted keys only.
        if isinstance(case["result"], dict) and isinstance(result, dict):
            assert all(result.get(k) == v for k, v in case["result"].items())
        else:
            assert case["result"] == result


@requires_exe
@pytest.mark.asyncio
async def test_channel_client_parity(csharp):
    client = _bound_channel(EchoStub())

    cases = {
        "submit:fills_instruction_from_operation":
            {"operation": "market_search"},
        "submit:update_shared_settings_keeps_empty":
            {"operation": "update_shared_settings"},
        "submit:no_operation_falls_back_status": {},
        "submit:explicit_instruction_kept":
            {"operation": "market_search", "instruction": "custom"},
    }
    # submit_instruction returns the stub echo; the echo's payload equals
    # the normalized request. The C# side emits the normalized request.
    for case_id, payload in cases.items():

        # Replay the Python normalization (same code path):
        request = dict(payload)
        operation = str(request.get("operation") or "").strip()
        if operation != "update_shared_settings" and not str(
            request.get("instruction") or ""
        ).strip():
            request["instruction"] = operation or "status"
        assert csharp[case_id] == request
        # And the real method returns the stub echo of that request.
        result = await client.submit_instruction(payload)
        assert result["echo"] == request

    assert csharp["request:unconnected"] == await ChannelClient(
        "x"
    )._request("c", {})


@requires_exe
@pytest.mark.asyncio
async def test_channel_send_parity(csharp):
    client = _bound_channel(EchoStub())
    for case_id, command in (
        ("send:snapshot", SNAPSHOT_COMMAND),
        ("send:instruction", INSTRUCTION_COMMAND),
        ("send:unowned", "other_command"),
    ):
        event, result = await client.send(
            command, {"operation": "market_search"}
        )
        assert csharp[case_id]["event"] == event
        assert csharp[case_id]["result"] == result

    failing = _bound_channel(FailingStub())
    event, result = await failing.send(SNAPSHOT_COMMAND, {})
    assert csharp["send:failing_channel"]["event"] == event
    assert csharp["send:failing_channel"]["result"] == result
