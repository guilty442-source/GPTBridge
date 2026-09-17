#!/usr/bin/env python3
"""End-to-end launch verification through the desktop EXE (official test path).

The only startup paths are the desktop EXE (official) and source startup
(``npm start``, development).  System-level testing — "does the main system
come up healthy" — MUST go through the desktop EXE so the exact end-user
launch chain is exercised:

    專案程式庫.exe -> venv pythonw -> launcher/scripts/start.py
    -> Electron -> boot_core -> backend (ready + UI connected)

The verifier launches the desktop EXE, waits for the governed readiness
endpoint (``/health`` with ``runtime_state == "ready"`` and an authenticated
UI connection), and prints the launcher journal evidence.  Exit code 0 means
the official startup path reached ready.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
JOURNAL_PATH = PROJECT_ROOT / "launcher" / "state" / "startup-journal.jsonl"
HEALTH_URL = "http://127.0.0.1:8765/health?brief=1"

DESKTOP_EXE = Path.home() / "Desktop" / "專案程式庫.exe"
INSTALLED_EXE = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "GPTBridgeLauncher"
    / "bin"
    / "專案程式庫.exe"
)


def desktop_exe() -> Path | None:
    for candidate in (DESKTOP_EXE, INSTALLED_EXE):
        if candidate.is_file():
            return candidate
    return None


def probe_health(timeout: float = 2.0) -> dict | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            urllib.request.Request(HEALTH_URL, headers={"Connection": "close"}),
            timeout=timeout,
        ) as response:
            return json.loads(response.read(65_537).decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def is_ready(payload: dict | None) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("runtime_state") == "ready"
        and payload.get("backend_runtime_ready") is True
        and payload.get("governance_ready") is True
        and payload.get("authenticated_ipc_connected") is True
    )


def journal_tail(count: int = 5) -> list[str]:
    try:
        lines = JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-count:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeout", type=float, default=180.0, help="readiness wait seconds"
    )
    parser.add_argument(
        "--cold",
        action="store_true",
        help="refuse to start when a backend is already running",
    )
    args = parser.parse_args()

    exe = desktop_exe()
    if exe is None:
        print("desktop EXE not installed; run launcher/scripts/install.py")
        return 2

    before = probe_health()
    if args.cold and before is not None:
        print(
            "refusing --cold verification: a backend is already running "
            "(close the main system first)"
        )
        return 2

    print(f"launching: {exe}")
    start = time.monotonic()
    subprocess.Popen(
        [str(exe)],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    deadline = start + max(10.0, args.timeout)
    payload: dict | None = None
    while time.monotonic() < deadline:
        payload = probe_health()
        if is_ready(payload):
            break
        time.sleep(0.5)
    elapsed = time.monotonic() - start

    for line in journal_tail():
        print(f"journal: {line}")

    if is_ready(payload):
        assert payload is not None
        print(
            "launch verification PASSED "
            f"({elapsed:.1f}s): runtime_state={payload.get('runtime_state')} "
            f"ui_connected={payload.get('authenticated_ipc_connected')}"
        )
        return 0

    state = (payload or {}).get("runtime_state", "unreachable")
    print(f"launch verification FAILED ({elapsed:.1f}s): runtime_state={state}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
