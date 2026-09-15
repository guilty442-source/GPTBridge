"""HTTP response helper for the IPC server (A185 split leaf).

Kept in its own module so ``server_lifecycle`` and
``server_lifecycle_health`` can share it without a circular import.
"""
from __future__ import annotations

from typing import Any

# Safe fallback for older websockets versions to prevent ImportError crashes
try:
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    def http_response(status_code: int, reason: str, body: bytes, content_type: str = "text/plain") -> Any:
        return Response(
            status_code,
            reason,
            Headers(
                [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(body))),
                ]
            ),
            body,
        )
except ImportError:
    import http
    def http_response(status_code: int, reason: str, body: bytes, content_type: str = "text/plain") -> Any:
        status = http.HTTPStatus(status_code)
        return (status, [("Content-Type", content_type), ("Content-Length", str(len(body)))], body)


__all__ = ["http_response"]
