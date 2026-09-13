"""IPC traffic metrics — lightweight counters for observability.

Records per-command invocation counts and per-event outbound counts/bytes
from the IPC hot paths (``process_command_task`` and ``UIShell.send_event``)
and flushes a snapshot to ``runtime/state/ipc-traffic-metrics.json`` at a
bounded interval.  The counters are in-memory integer accumulations; the
flush is the only I/O and is throttled, so instrumentation adds negligible
overhead to the event loop.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final


_FLUSH_INTERVAL_SECONDS: Final[float] = 5.0
_START = time.monotonic()
_last_flush: float = 0.0
_commands: dict[str, int] = {}
_events: dict[str, list[int]] = {}
_diag: dict[str, list[dict[str, object]]] = {}


def record_diag(kind: str, value: object) -> None:
    """Keep the last few samples of a diagnostic value (bounded, cheap)."""
    row = _diag.setdefault(str(kind), [])
    row.append({"t": round(time.monotonic() - _START, 2), "v": value})
    if len(row) > 8:
        del row[:-8]
    _maybe_flush()


def _metrics_file() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "runtime"
        / "state"
        / "ipc-traffic-metrics.json"
    )


def note_command(command: str) -> None:
    """Count one inbound command execution."""
    key = str(command or "")
    _commands[key] = _commands.get(key, 0) + 1
    _maybe_flush()


def note_event(event: str, size_bytes: int) -> None:
    """Count one outbound UI event and its serialized byte size."""
    key = str(event or "")
    row = _events.get(key)
    if row is None:
        _events[key] = [1, int(size_bytes)]
    else:
        row[0] += 1
        row[1] += int(size_bytes)
    _maybe_flush()


def _maybe_flush() -> None:
    global _last_flush
    now = time.monotonic()
    if now - _last_flush < _FLUSH_INTERVAL_SECONDS:
        return
    _last_flush = now
    try:
        payload = {
            "uptime_seconds": round(now - _START, 1),
            "commands": dict(
                sorted(_commands.items(), key=lambda item: -item[1])
            ),
            "events": {
                key: {"count": row[0], "bytes": row[1]}
                for key, row in sorted(
                    _events.items(), key=lambda item: -item[1][1]
                )
            },
            "diag": {key: list(row) for key, row in _diag.items()},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = _metrics_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


__all__ = ["note_command", "note_event", "record_diag"]
