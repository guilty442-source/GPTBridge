from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets


sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

import ipc.server as ipc_server  # noqa: E402
from ipc.server import (  # noqa: E402
    DEFAULT_IPC_PORT,
    IPC_PORT_ENV,
    IPC_SESSION_TOKEN_ENV,
    IPC_STATE_ROOT_ENV,
    SHUTDOWN_TOKEN_ENV,
    STANDALONE_TOOL_ID_ENV,
    TRUSTED_WEBSOCKET_ORIGINS,
    _get_or_create_ipc_session_token,
    _ipc_port,
    _ipc_session_token_file,
    _shutdown_request_authorized,
    _shutdown_request_is_manual,
    _standalone_tool_id,
    _websocket_request_authorized,
    _workspace_instance_id,
    http_response,
)


def test_ipc_port_defaults_and_rejects_invalid_overrides(monkeypatch) -> None:
    monkeypatch.delenv(IPC_PORT_ENV, raising=False)
    assert _ipc_port() == DEFAULT_IPC_PORT

    monkeypatch.setenv(IPC_PORT_ENV, "21265")
    assert _ipc_port() == 21265

    for invalid in ("not-a-port", "80", "65536"):
        monkeypatch.setenv(IPC_PORT_ENV, invalid)
        with pytest.raises(RuntimeError, match=IPC_PORT_ENV):
            _ipc_port()


def test_standalone_tool_scope_rejects_invalid_identifiers(monkeypatch) -> None:
    monkeypatch.delenv(STANDALONE_TOOL_ID_ENV, raising=False)
    assert _standalone_tool_id() == ""

    monkeypatch.setenv(STANDALONE_TOOL_ID_ENV, "ai-assistant")
    assert _standalone_tool_id() == "ai-assistant"

    for invalid in ("AI Assistant", "../file-sorter", "tool:other"):
        monkeypatch.setenv(STANDALONE_TOOL_ID_ENV, invalid)
        with pytest.raises(RuntimeError, match=STANDALONE_TOOL_ID_ENV):
            _standalone_tool_id()


def test_websocket_origins_allow_desktop_and_local_development_only() -> None:
    assert None in TRUSTED_WEBSOCKET_ORIGINS
    assert "file://" in TRUSTED_WEBSOCKET_ORIGINS
    assert "http://127.0.0.1:5180" in TRUSTED_WEBSOCKET_ORIGINS
    assert "http://127.0.0.1:5183" in TRUSTED_WEBSOCKET_ORIGINS
    assert "https://example.com" not in TRUSTED_WEBSOCKET_ORIGINS


def test_shutdown_requires_matching_environment_token(monkeypatch) -> None:
    request = SimpleNamespace(
        headers={"X-GPTBridge-Shutdown-Token": "expected-token"}
    )

    monkeypatch.delenv(SHUTDOWN_TOKEN_ENV, raising=False)
    assert _shutdown_request_authorized(request) is False

    monkeypatch.setenv(SHUTDOWN_TOKEN_ENV, "different-token")
    assert _shutdown_request_authorized(request) is False

    monkeypatch.setenv(SHUTDOWN_TOKEN_ENV, "expected-token")
    assert _shutdown_request_authorized(request) is True


def test_hot_reload_shutdown_does_not_disable_auto_start() -> None:
    assert _shutdown_request_is_manual(
        SimpleNamespace(
            headers={"X-GPTBridge-Shutdown-Reason": "hot-reload"}
        )
    ) is False
    assert _shutdown_request_is_manual(SimpleNamespace(headers={})) is True


def test_websocket_commands_require_matching_session_token(
    monkeypatch,
    tmp_path,
) -> None:
    expected = "a" * 64
    monkeypatch.setenv(IPC_SESSION_TOKEN_ENV, expected)
    monkeypatch.setenv("GPTBRIDGE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(ipc_server, "_IPC_SESSION_TOKEN", None)
    instance_id = _workspace_instance_id()

    assert _websocket_request_authorized(
        SimpleNamespace(path=f"/?token={expected}&instance={instance_id}")
    ) is True
    assert _websocket_request_authorized(SimpleNamespace(path="/")) is False
    assert _websocket_request_authorized(
        SimpleNamespace(path=f"/?token=wrong&instance={instance_id}")
    ) is False
    assert _websocket_request_authorized(
        SimpleNamespace(path=f"/?token={expected}&instance=wrong")
    ) is False
    assert _websocket_request_authorized(
        SimpleNamespace(path=f"/?token={expected}")
    ) is False


def test_ipc_session_token_reuses_valid_per_user_state_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "isolated-ipc-state"
    expected = "c" * 64
    state_root.mkdir()
    (state_root / "session-token").write_text(expected + "\n", encoding="utf-8")
    monkeypatch.setenv(IPC_STATE_ROOT_ENV, str(state_root))
    monkeypatch.delenv(IPC_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.setattr(ipc_server, "_IPC_SESSION_TOKEN", None)

    assert _ipc_session_token_file() == state_root.resolve() / "session-token"
    assert _get_or_create_ipc_session_token() == expected

    monkeypatch.setattr(ipc_server, "_IPC_SESSION_TOKEN", None)
    assert _get_or_create_ipc_session_token() == expected
    assert (state_root / "session-token").read_text(encoding="utf-8").strip() == expected


def test_invalid_ipc_session_token_is_quarantined_and_repaired(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "isolated-ipc-state"
    state_root.mkdir()
    token_path = state_root / "session-token"
    token_path.write_text("not-a-valid-capability\n", encoding="utf-8")
    monkeypatch.setenv(IPC_STATE_ROOT_ENV, str(state_root))
    monkeypatch.delenv(IPC_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.setattr(ipc_server, "_IPC_SESSION_TOKEN", None)

    repaired = _get_or_create_ipc_session_token()

    assert re.fullmatch(r"[a-f0-9]{64}", repaired)
    assert token_path.read_text(encoding="utf-8").strip() == repaired
    quarantined = list(state_root.glob("session-token.invalid-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8").strip() == "not-a-valid-capability"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing violation regression")
def test_ipc_token_repair_retries_transient_windows_sharing_violation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "sharing-violation-ipc-state"
    state_root.mkdir()
    token_path = state_root / "session-token"
    token_path.write_text("broken\n", encoding="utf-8")
    monkeypatch.setenv(IPC_STATE_ROOT_ENV, str(state_root))
    monkeypatch.delenv(IPC_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.setattr(ipc_server, "_IPC_SESSION_TOKEN", None)
    real_replace = ipc_server.os.replace
    attempts = 0

    def replace_with_one_sharing_violation(source, target) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(13, "simulated Windows sharing violation")
        real_replace(source, target)

    monkeypatch.setattr(ipc_server.os, "replace", replace_with_one_sharing_violation)

    repaired = _get_or_create_ipc_session_token()

    assert attempts >= 3
    assert re.fullmatch(r"[a-f0-9]{64}", repaired)
    assert token_path.read_text(encoding="utf-8").strip() == repaired


def test_stale_ipc_token_repair_lock_self_heals(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "stale-lock-ipc-state"
    lock_path = state_root / ".session-token.lock"
    lock_path.mkdir(parents=True)
    (lock_path / "owner").write_text("abandoned-owner\n", encoding="ascii")
    stale_timestamp = time.time() - 60
    os.utime(lock_path, (stale_timestamp, stale_timestamp))
    monkeypatch.setenv(IPC_STATE_ROOT_ENV, str(state_root))
    monkeypatch.delenv(IPC_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.setattr(ipc_server, "_IPC_SESSION_TOKEN", None)

    repaired = _get_or_create_ipc_session_token()

    assert re.fullmatch(r"[a-f0-9]{64}", repaired)
    assert (state_root / "session-token").read_text(encoding="utf-8").strip() == repaired
    assert not lock_path.exists()
    assert list(state_root.glob(".session-token.lock.stale-*"))


def test_parallel_ipc_session_token_repair_converges_on_one_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "parallel-ipc-state"
    state_root.mkdir()
    (state_root / "session-token").write_text("broken\n", encoding="utf-8")
    monkeypatch.delenv(IPC_SESSION_TOKEN_ENV, raising=False)

    environment = os.environ.copy()
    environment.pop(IPC_SESSION_TOKEN_ENV, None)
    environment[IPC_STATE_ROOT_ENV] = str(state_root)
    worker = "\n".join(
        [
            "import sys",
            "sys.path.insert(0, 'src-core')",
            "from ipc.server import _get_or_create_ipc_session_token",
            "print(_get_or_create_ipc_session_token(), flush=True)",
        ]
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker],
            cwd=os.getcwd(),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]

    results: list[str] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(stdout.strip())

    assert len(set(results)) == 1
    assert re.fullmatch(r"[a-f0-9]{64}", results[0])
    assert (state_root / "session-token").read_text(encoding="utf-8").strip() == results[0]
    assert not (state_root / ".session-token.lock").exists()


def test_workspace_instance_id_is_stable_and_does_not_expose_path(
    tmp_path: Path,
) -> None:
    normalized = os.path.normcase(str(tmp_path.absolute())).replace("\\", "/")
    expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    instance_id = _workspace_instance_id(tmp_path)

    assert instance_id == expected
    assert str(tmp_path) not in instance_id
    assert re.fullmatch(r"[a-f0-9]{24}", instance_id)


@pytest.mark.asyncio
async def test_websocket_server_rejects_untrusted_browser_origin() -> None:
    async def handler(connection) -> None:
        await connection.send("ready")

    async with websockets.serve(
        handler,
        "127.0.0.1",
        0,
        origins=TRUSTED_WEBSOCKET_ORIGINS,
    ) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"

        async with websockets.connect(url) as connection:
            assert await connection.recv() == "ready"

        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(
                url,
                origin="https://example.com",
            ):
                pass


@pytest.mark.asyncio
async def test_websocket_upgrade_requires_session_capability(
    monkeypatch,
    tmp_path,
) -> None:
    expected = "b" * 64
    monkeypatch.setenv(IPC_SESSION_TOKEN_ENV, expected)
    monkeypatch.setenv("GPTBRIDGE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(ipc_server, "_IPC_SESSION_TOKEN", None)
    instance_id = _workspace_instance_id()

    async def handler(connection) -> None:
        await connection.send("ready")

    def authorize(_connection, request):
        if not _websocket_request_authorized(request):
            return http_response(403, "FORBIDDEN", b"Forbidden")
        return None

    async with websockets.serve(
        handler,
        "127.0.0.1",
        0,
        process_request=authorize,
    ) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"

        async with websockets.connect(
            f"{url}/?token={expected}&instance={instance_id}"
        ) as connection:
            assert await connection.recv() == "ready"

        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(url):
                pass

        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(
                f"{url}/?token=wrong&instance={instance_id}"
            ):
                pass

        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(
                f"{url}/?token={expected}&instance=wrong"
            ):
                pass
