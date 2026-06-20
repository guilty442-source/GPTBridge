from __future__ import annotations

import socket
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 2.0


def test_connection(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Test whether a TCP endpoint can be reached."""

    if not host:
        return {"ok": False, "host": host, "port": port, "message": "host is required"}
    try:
        target_port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "host": host, "port": port, "message": "port must be an integer"}
    if target_port <= 0 or target_port > 65535:
        return {"ok": False, "host": host, "port": target_port, "message": "port out of range"}

    try:
        with socket.create_connection((host, target_port), timeout=timeout_seconds):
            return {
                "ok": True,
                "host": host,
                "port": target_port,
                "message": "connection ok",
            }
    except OSError as exc:
        return {
            "ok": False,
            "host": host,
            "port": target_port,
            "message": str(exc),
        }
