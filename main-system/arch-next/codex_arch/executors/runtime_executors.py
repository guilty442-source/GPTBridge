"""runtime executors — 進程存續與運行期完整性（stdlib subprocess/os）。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..governance.delegation import ExecutorBinding

_managed_processes: dict[str, int] = {}


def _probe_process(payload: dict[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("target") or os.getpid())
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return {"alive": False, "reason": "invalid-pid"}
    alive = True
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        alive = False
    return {"pid": pid, "alive": alive}


def _spawn_managed(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "managed-runtime")
    if name in _managed_processes:
        return {"spawned": False, "reason": "already-managed", "name": name}
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    _managed_processes[name] = process.pid  # type: ignore[attr-defined]
    pid: int = process.pid or 0
    return {"spawned": True, "name": name, "pid": pid, "executor": "spawn-managed-process"}


def _stop_managed(payload: dict[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("target") or "")
    stopped: list[dict[str, Any]] = []
    for name, pid in list(_managed_processes.items()):
        if raw and name != raw:
            continue
        try:
            os.kill(pid, 9 if os.name == "nt" else 15)
        except (OSError, ProcessLookupError):
            stopped.append({"name": name, "pid": pid, "ok": False})
        else:
            stopped.append({"name": name, "pid": pid, "ok": True})
        _managed_processes.pop(name, None)
    return {"stopped": stopped, "remaining": len(_managed_processes)}


def _runtime_integrity(payload: dict[str, Any]) -> dict[str, Any]:
    guard = bool(payload.get("guard"))
    sender = Path(__file__).resolve().parents[2]
    fingerprint = hashlib.sha256()
    for marker in ("manifest.py", "__init__.py"):
        candidate = sender / marker
        if candidate.is_file():
            fingerprint.update(candidate.read_bytes())
    return {
        "guard": guard,
        "fingerprint": fingerprint.hexdigest()[:16],
        "executor": "runtime-integrity",
    }


def bindings() -> list[ExecutorBinding]:
    return [
        ExecutorBinding(
            executor_id="probe-process",
            boundary="process-survival",
            permission_intent="probe-process",
            owner_sovereign="runtime",
            target="runtime-sovereign:runtime:probe-process",
            implementation=_probe_process,
        ),
        ExecutorBinding(
            executor_id="spawn-managed-process",
            boundary="process-survival",
            permission_intent="spawn-managed-process",
            owner_sovereign="runtime",
            target="runtime-sovereign:runtime:spawn-managed-process",
            implementation=_spawn_managed,
        ),
        ExecutorBinding(
            executor_id="stop-managed-process",
            boundary="process-survival",
            permission_intent="stop-managed-process",
            owner_sovereign="runtime",
            target="runtime-sovereign:runtime:stop-managed-process",
            implementation=_stop_managed,
        ),
        ExecutorBinding(
            executor_id="runtime-integrity",
            boundary="runtime-integrity",
            permission_intent="runtime-integrity",
            owner_sovereign="runtime",
            target="runtime-sovereign:runtime:runtime-integrity",
            implementation=_runtime_integrity,
        ),
    ]


__all__ = ["bindings"]