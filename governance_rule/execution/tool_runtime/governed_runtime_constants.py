"""Shared constants and helpers for governed runtime (A185 split).

Kept here to avoid circular imports between governed_runtime.py and
its mixin modules.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

BOOTSTRAP_ENV: Final[str] = "GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP"
TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-f0-9]{64}$")
Executor = Callable[[str, dict[str, Any], str], Awaitable[tuple[str, dict[str, Any]]]]
Lifecycle = Callable[[], Awaitable[Any]]
Cancellation = Callable[[str], Awaitable[bool]]
MAX_OUTPUT_CHARACTERS: Final[int] = 2 * 1024 * 1024
LOCAL_CLEANUP_COMMAND: Final[str] = "toolbox_run_local_cleanup"
GOVERNANCE_MAIN_ACTOR: Final[str] = "governance/main-system"


def background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def permission_denied() -> PermissionError:
    return PermissionError("PERMISSION_DENIED")


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def http_response(status: int, reason: str, body: bytes, content_type: str):
    try:
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        return Response(
            status,
            reason,
            Headers(
                [("Content-Type", content_type), ("Content-Length", str(len(body)))],
            ),
            body,
        )
    except ImportError:
        import http

        return (
            http.HTTPStatus(status),
            [("Content-Type", content_type), ("Content-Length", str(len(body)))],
            body,
        )


__all__ = [
    "BOOTSTRAP_ENV",
    "TOKEN_PATTERN",
    "Executor",
    "Lifecycle",
    "Cancellation",
    "MAX_OUTPUT_CHARACTERS",
    "LOCAL_CLEANUP_COMMAND",
    "GOVERNANCE_MAIN_ACTOR",
    "background_subprocess_kwargs",
    "permission_denied",
    "iso_now",
    "http_response",
]
