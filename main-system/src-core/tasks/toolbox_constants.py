"""Module-level constants and helper functions for the toolbox service.

These are shared across all toolbox mixin modules and the main
``ToolboxService`` composition class.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Awaitable, Callable, Dict

ToolEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]

MAX_TOOL_ARGUMENTS = 256
MAX_TOOL_ARGUMENT_BYTES = 256 * 1024
MAX_TOOL_REQUEST_ID_LENGTH = 128
MAX_TOOL_OUTPUT_CHARS = 2 * 1024 * 1024
MAX_TOOL_STREAM_LINE_BYTES = 4 * 1024 * 1024
_MANAGED_BACKEND_TOOL_ID_ENV = "GPTBRIDGE_MANAGED_BACKEND_TOOL_ID"
_MANAGED_BACKEND_WORKSPACE_ID_ENV = "GPTBRIDGE_MANAGED_BACKEND_WORKSPACE_INSTANCE_ID"
_MANAGED_BACKEND_VERSION_ENV = "GPTBRIDGE_MANAGED_BACKEND_VERSION"
_TOOL_GOVERNANCE_BOOTSTRAP_ENV = "GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP"
_REQUIRED_TOOL_VERSION = "1.0.0"
_REQUIRED_TOOL_DISPLAY_VERSION = "1.0"

_TOOL_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "GPTBRIDGE_POSTGRES_DSN",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USERPROFILE",
        "WINDIR",
        "XDG_STATE_HOME",
    }
)
_TOOL_ENVIRONMENT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
_FORBIDDEN_TOOL_ENVIRONMENT_MARKERS = frozenset(
    {"API_KEY", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN"}
)


def _is_declarable_tool_environment_key(value: Any) -> bool:
    key = str(value or "").strip().upper()
    return (
        _TOOL_ENVIRONMENT_KEY_PATTERN.fullmatch(key) is not None
        and key.startswith(("GPTBRIDGE_", "FILE_SORTER_"))
        and not any(marker in key for marker in _FORBIDDEN_TOOL_ENVIRONMENT_MARKERS)
    )


def _background_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    # Independent tools must survive a main-system crash or restart.
    # DETACHED_PROCESS removes the console parent/child coupling and
    # CREATE_NEW_PROCESS_GROUP isolates Ctrl+C/Ctrl+Break handling, so
    # terminating the main-system (or boot_core/main.py) does not cascade
    # through to independent tool processes.  CREATE_NO_WINDOW keeps them
    # headless.
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
    creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
    if not creationflags:
        return {}
    return {"creationflags": creationflags}


def _run_hidden_subprocess(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 4,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        **_background_subprocess_kwargs(),
    )
