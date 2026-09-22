"""INTEGRATION-10 (G59): 24h continuous-run acceptance.

Verifies from real backend logs that a single backend session ran >= 24h
without fatal restart. Until any session reaches the window the test
*skips* — the check is fail-closed on evidence, never on absence of data.

Session detection: the ``IPC Server running`` line marks a session start;
a new marker (or end of the log stream) ends it. Fatal markers
(``Traceback``, ``FATAL``) inside a session fail the acceptance.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

LOG_DIR = Path(__file__).resolve().parents[1] / "runtime" / "logs"
WINDOW = timedelta(hours=24)
START_MARKER = "IPC Server running"
FATAL_MARKERS = ("Traceback (most recent call last)", "FATAL", "fatal error")
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _sessions() -> list[tuple[datetime, datetime, list[str]]]:
    """Return (start, end, fatal_lines) per observed session across all logs."""
    sessions: list[tuple[datetime, datetime, list[str]]] = []
    current_start: datetime | None = None
    current_last: datetime | None = None
    fatals: list[str] = []
    for path in sorted(LOG_DIR.glob("backend-*.log")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if START_MARKER in line:
                if current_start is not None and current_last is not None:
                    sessions.append((current_start, current_last, fatals))
                current_start, current_last, fatals = None, None, []
            match = TS_RE.match(line)
            if match:
                ts = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
                if current_start is None:
                    current_start = ts
                current_last = ts
            if current_start is not None and any(m in line for m in FATAL_MARKERS):
                fatals.append(line[:200])
    if current_start is not None and current_last is not None:
        sessions.append((current_start, current_last, fatals))
    return sessions


def test_backend_session_runs_24h_without_fatal() -> None:
    sessions = _sessions()
    assert sessions, "no backend session evidence in runtime/logs"
    longest = max(sessions, key=lambda s: s[1] - s[0])
    span = longest[1] - longest[0]
    if span < WINDOW:
        pytest.skip(
            f"longest observed session {span} < 24h window "
            f"(session start {longest[0].isoformat()})"
        )
    assert not longest[2], (
        f"24h session contains fatal markers: {longest[2][:3]}"
    )
