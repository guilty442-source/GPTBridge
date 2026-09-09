from __future__ import annotations

from typing import Any, Callable

PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_PORT = 18765
DEFAULT_PAIRING_TTL_HOURS = 1.0
REQUEST_WINDOW_SECONDS = 60.0
MAX_REQUESTS_PER_WINDOW = 120
SESSION_TTL_HOURS = 12.0
SESSION_IDLE_SECONDS = 30 * 60
SnapshotProvider = Callable[[], dict[str, Any]]
CommandScheduler = Callable[[str], dict[str, Any]]
RemoteUrlProvider = Callable[[], str]
