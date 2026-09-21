"""G85 one-time Renderer WebSocket ticket boundary tests."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from urllib.parse import quote

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from ipc import server_tokens
from ipc.server_http import http_response


class _Request:
    def __init__(self, path: str) -> None:
        self.path = path


def _ticket(token: str, instance: str, *, expires: int | None = None) -> str:
    payload = f"{expires or int(time.time()) + 30}.nonce-{time.time_ns()}.{instance}"
    signature = hmac.new(
        token.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def test_ticket_is_workspace_bound_and_one_time(monkeypatch) -> None:
    token = "a" * 64
    instance = "instance-1"
    monkeypatch.setattr(server_tokens, "_get_or_create_ipc_session_token", lambda: token)
    monkeypatch.setattr(server_tokens, "_workspace_instance_id", lambda: instance)
    server_tokens._USED_WS_TICKET_NONCES.clear()

    ticket = _ticket(token, instance)
    request = _Request(
        f"/?ticket={quote(ticket)}&instance={quote(instance)}"
    )
    assert server_tokens._websocket_request_authorized(request) is True
    assert server_tokens._websocket_request_authorized(request) is False


def test_ticket_rejects_expiry_signature_and_instance(monkeypatch) -> None:
    token = "b" * 64
    instance = "instance-2"
    monkeypatch.setattr(server_tokens, "_get_or_create_ipc_session_token", lambda: token)
    monkeypatch.setattr(server_tokens, "_workspace_instance_id", lambda: instance)
    server_tokens._USED_WS_TICKET_NONCES.clear()

    expired = _ticket(token, instance, expires=int(time.time()) - 1)
    assert not server_tokens._websocket_request_authorized(
        _Request(f"/?ticket={quote(expired)}&instance={instance}")
    )

    valid = _ticket(token, instance)
    bad_signature = valid[:-1] + ("0" if valid[-1] != "0" else "1")
    assert not server_tokens._websocket_request_authorized(
        _Request(f"/?ticket={quote(bad_signature)}&instance={instance}")
    )
    assert not server_tokens._websocket_request_authorized(
        _Request(f"/?ticket={quote(valid)}&instance=other")
    )


async def _socket_handshake(port: int, path: str) -> bool:
    """True when the upgrade succeeds; False on the gated 403."""
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}{path}", open_timeout=5
        ) as ws:
            await ws.ping()
        return True
    except InvalidStatus as error:
        assert error.response.status_code == 403
        return False


@pytest.mark.asyncio
async def test_real_handshake_accepts_then_rejects_replay(monkeypatch) -> None:
    """Socket-level E2E: the production gate decides inside a real upgrade."""
    token = "c" * 64
    instance = "instance-e2e"
    monkeypatch.setattr(
        server_tokens, "_get_or_create_ipc_session_token", lambda: token
    )
    monkeypatch.setattr(
        server_tokens, "_workspace_instance_id", lambda: instance
    )
    server_tokens._USED_WS_TICKET_NONCES.clear()

    async def gate(_connection, request):
        if server_tokens._websocket_request_authorized(request):
            return None
        return http_response(403, "FORBIDDEN", b"Forbidden")

    async def echo(ws):
        async for _ in ws:
            pass

    async with websockets.serve(echo, "127.0.0.1", 0, process_request=gate) as srv:
        port = srv.sockets[0].getsockname()[1]

        ticket = _ticket(token, instance)
        path = f"/?ticket={quote(ticket)}&instance={quote(instance)}"
        assert await _socket_handshake(port, path) is True
        # Replay of the consumed nonce is denied at the handshake.
        assert await _socket_handshake(port, path) is False
        # No credentials at all: denied.
        assert await _socket_handshake(port, f"/?instance={instance}") is False
        # Legacy governed-token path still admitted (non-renderer clients).
        legacy = f"/?token={quote(token)}&instance={quote(instance)}"
        assert await _socket_handshake(port, legacy) is True
        # Tampered ticket payload: denied.
        forged = ticket[:-1] + ("0" if ticket[-1] != "0" else "1")
        forged_path = f"/?ticket={quote(forged)}&instance={quote(instance)}"
        assert await _socket_handshake(port, forged_path) is False
