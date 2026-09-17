"""Governed lifecycle control for Xingcheng's native Ollama model."""

from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .xingcheng_native_model_constants import _MAIN_MODEL_ID

_OLLAMA_API = "http://127.0.0.1:11434/api"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_ollama(*args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ollama", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def native_model_status() -> dict[str, Any]:
    """Return live process state; static registration never implies running."""
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        result = _run_ollama("ps")
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "model_id": _MAIN_MODEL_ID,
            "state": "unavailable",
            "running": False,
            "available": False,
            "checked_at": checked_at,
            "message": type(exc).__name__,
        }
    if result.returncode != 0:
        return {
            "model_id": _MAIN_MODEL_ID,
            "state": "unavailable",
            "running": False,
            "available": False,
            "checked_at": checked_at,
            "message": (result.stderr or "ollama unavailable").strip(),
        }
    running = any(
        line.split(maxsplit=1)[0] == _MAIN_MODEL_ID
        for line in result.stdout.splitlines()[1:]
        if line.strip()
    )
    return {
        "model_id": _MAIN_MODEL_ID,
        "state": "running" if running else "stopped",
        "running": running,
        "available": True,
        "checked_at": checked_at,
        "message": "",
    }


def _start_native_model() -> None:
    payload = json.dumps(
        {"model": _MAIN_MODEL_ID, "prompt": "", "stream": False, "keep_alive": "30m"}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{_OLLAMA_API}/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"Ollama returned HTTP {response.status}")


def _stop_native_model() -> None:
    result = _run_ollama("stop", _MAIN_MODEL_ID, timeout=30.0)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "model stop failed").strip())


async def set_native_model_enabled(enabled: bool) -> dict[str, Any]:
    """Apply a user-requested lifecycle transition and verify the result."""
    action = _start_native_model if enabled else _stop_native_model
    try:
        await asyncio.to_thread(action)
    except (OSError, RuntimeError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        status = await asyncio.to_thread(native_model_status)
        return {"ok": False, "error_code": "NATIVE_MODEL_TRANSITION_FAILED", "message": str(exc), **status}
    status = await asyncio.to_thread(native_model_status)
    expected = status["running"] is enabled
    return {
        "ok": expected,
        "error_code": "" if expected else "NATIVE_MODEL_STATE_MISMATCH",
        "message": "" if expected else "native model state verification failed",
        **status,
    }


__all__ = ["native_model_status", "set_native_model_enabled"]
