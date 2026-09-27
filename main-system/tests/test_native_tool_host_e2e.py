"""End-to-end smoke: spawn the REAL SystemRescue.Host.exe + the REAL
transport-proxy agent (wire fixture, file-backed queue) and drive one
governed request through claim -> execute -> respond -> /shutdown.

Requires a published ``dist/SystemRescue.Host.exe``; skipped otherwise.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = WORKSPACE_ROOT / "Standalone tools" / "system-rescue"
HOST_EXE = TOOL_DIR / "dist" / "SystemRescue.Host.exe"
WIRE_AGENT = WORKSPACE_ROOT / "native" / "test_suites" / "proxy_wire_agent.py"
PYTHON = WORKSPACE_ROOT / "main-system" / ".venv" / "Scripts" / "python.exe"

pytestmark = pytest.mark.skipif(
    not HOST_EXE.is_file() or not PYTHON.is_file(),
    reason="SystemRescue.Host.exe not published on this machine",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wait_http(url: str, timeout: float = 15.0, headers: dict | None = None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with _OPENER.open(req, timeout=0.75) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:
            time.sleep(0.15)
    return None, None


def test_native_tool_host_e2e(tmp_path: Path):
    queue_dir = tmp_path / "wire-queue"
    queue_dir.mkdir()
    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT": str(WORKSPACE_ROOT),
            "GPTBRIDGE_TOOL_DIR": str(TOOL_DIR),
            "GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID": "system-rescue",
            "GPTBRIDGE_IPC_SESSION_TOKEN": secrets.token_hex(32),
            "GPTBRIDGE_IPC_PORT": str(port),
            "GPTBRIDGE_SHUTDOWN_TOKEN": secrets.token_hex(32),
            "GPTBRIDGE_TOOLHOST_PROXY_ENTRY": str(WIRE_AGENT),
            "GPTBRIDGE_WIRE_QUEUE": str(queue_dir),
            "GPTBRIDGE_WIRE_REQUESTER_ACTOR": "governance/main-system",
        }
    )
    proc = subprocess.Popen(
        [str(HOST_EXE)],
        cwd=str(TOOL_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # 1. /health reports governed readiness with the right identity.
        status, body = _wait_http(f"http://127.0.0.1:{port}/health")
        assert status == 200, "health endpoint never came up"
        health = json.loads(body)
        assert health["ok"] is True
        assert health["governance_ready"] is True
        assert health["tool_id"] == "system-rescue"
        assert health["runtime_host"] == "csharp-toolhost"
        assert len(health["workspace_instance_id"]) == 24

        # 2. Queue one governed request through the wire queue.
        request_id = f"req-{secrets.token_hex(8)}"
        (queue_dir / f"req-{request_id}.json").write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "requester_actor": "governance/main-system",
                    "target_tool_id": "system-rescue",
                    "payload": {
                        "_governed_command": "system_health_check"
                    },
                    "status": "queued",
                    "attempt_count": 0,
                }
            ),
            encoding="utf-8",
        )
        done_path = queue_dir / f"done-{request_id}.json"
        deadline = time.monotonic() + 20
        while not done_path.exists() and time.monotonic() < deadline:
            assert proc.poll() is None, "host exited early"
            time.sleep(0.2)
        assert done_path.exists(), "request never completed"
        row = json.loads(done_path.read_text(encoding="utf-8"))
        response = row["response"]
        assert response["ok"] is True
        assert response["tool_id"] == "system-rescue"
        assert response["request_id"] == request_id
        assert (
            response["authority"] == "main-system-central-packaging-only"
        )

        # 3. /metrics surfaces channel health.
        status, body = _wait_http(f"http://127.0.0.1:{port}/metrics", 5)
        assert status == 200
        metrics = json.loads(body)
        assert "channel_health" in metrics

        # 4. /shutdown requires the token and then exits the process.
        status, _ = _wait_http(
            f"http://127.0.0.1:{port}/shutdown",
            5,
            headers={"X-GPTBridge-Shutdown-Token": "wrong"},
        )
        assert status == 403
        assert proc.poll() is None
        status, _ = _wait_http(
            f"http://127.0.0.1:{port}/shutdown",
            5,
            headers={
                "X-GPTBridge-Shutdown-Token": env["GPTBRIDGE_SHUTDOWN_TOKEN"]
            },
        )
        assert status == 200
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
