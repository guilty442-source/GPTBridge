"""Diagnostics and Monitoring.

Loop stall watchdog, logging utilities, and diagnostic helpers.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def start_loop_stall_watchdog(app: Any) -> None:
    """Start the event loop stall watchdog.

    Enabled only when GPTBRIDGE_LOOP_STALL_DEBUG is set. A heartbeat
    task marks time on the event loop; a daemon thread watches for gaps
    larger than GPTBRIDGE_LOOP_STALL_MS (default 300) and appends the
    main loop thread's live stack to loop-stall.txt.
    """
    loop_thread = threading.get_ident()
    heartbeat = {"t": time.monotonic()}
    threshold = (
        float(os.environ.get("GPTBRIDGE_LOOP_STALL_MS", "300")) / 1000.0
    )
    stall_log = (
        Path(os.environ.get("GPTBRIDGE_PROJECT_ROOT", ".")) / "main-system" / "runtime" / "logs" / "loop-stall.txt"
    )

    async def _beat() -> None:
        while True:
            heartbeat["t"] = time.monotonic()
            await asyncio.sleep(0.05)

    def _watch() -> None:
        while True:
            time.sleep(0.1)
            lag = time.monotonic() - heartbeat["t"]
            if lag < threshold:
                continue
            frame = sys._current_frames().get(loop_thread)
            stack = (
                "".join(traceback.format_stack(frame))
                if frame is not None
                else "<no frame>\n"
            )
            try:
                stall_log.parent.mkdir(parents=True, exist_ok=True)
                with stall_log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "=== stall "
                        f"{lag:.3f}s at "
                        f"{datetime.now(timezone.utc).isoformat()} ===\n"
                        f"{stack}\n"
                    )
            except OSError:
                pass
            heartbeat["t"] = time.monotonic()

    asyncio.get_event_loop().create_task(_beat())
    threading.Thread(
        target=_watch, daemon=True, name="loop-stall-watchdog"
    ).start()


def log_structured(data: dict[str, Any]) -> None:
    """Log structured JSON data to stdout."""
    print(json.dumps(data, ensure_ascii=False), flush=True)


def record_failure(stage: str, error: BaseException, startup_failures: list[dict[str, Any]]) -> None:
    """Record a stage failure for single-fault isolation."""
    failure = {
        "stage": stage,
        "error": f"{type(error).__name__}: {error}",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    startup_failures.append(failure)
    log_structured({"type": "startup_failure", **failure})


__all__ = [
    "start_loop_stall_watchdog",
    "log_structured",
    "record_failure",
]