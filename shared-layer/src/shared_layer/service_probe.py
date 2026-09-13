from __future__ import annotations

import socket
import time
from dataclasses import asdict, dataclass
from typing import Final


REGISTERED_LOCAL_SERVICES: Final[dict[str, tuple[str, int]]] = {
    "postgresql": ("127.0.0.1", 5432),
    "qdrant": ("127.0.0.1", 6333),
    "ollama": ("127.0.0.1", 11434),
}

_PROBE_CACHE_TTL_SECONDS: Final[float] = 5.0
_probe_cache: dict[str, tuple[float, "ServiceProbeResult"]] = {}


@dataclass(frozen=True)
class ServiceProbeResult:
    service: str
    host: str
    port: int
    reachable: bool
    transport_owner: str = "shared-layer"
    channel: str = "system"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def probe_registered_local_service(
    service: str, *, timeout: float = 0.75
) -> ServiceProbeResult:
    """Probe a registered dependency through the information-layer boundary.

    Results are cached for a few seconds: readiness and status surfaces
    re-probe every registered dependency on each evaluation, and the
    per-request socket connects dominated the backend event loop when the
    UI polled status.
    """
    normalized = str(service or "").strip().casefold()
    endpoint = REGISTERED_LOCAL_SERVICES.get(normalized)
    if endpoint is None:
        raise PermissionError("UNREGISTERED_INFORMATION_CHANNEL")
    now = time.monotonic()
    cached = _probe_cache.get(normalized)
    if cached is not None and now - cached[0] < _PROBE_CACHE_TTL_SECONDS:
        return cached[1]
    host, port = endpoint
    try:
        with socket.create_connection((host, port), timeout=max(0.05, timeout)):
            reachable = True
    except OSError:
        reachable = False
    result = ServiceProbeResult(normalized, host, port, reachable)
    _probe_cache[normalized] = (now, result)
    return result


__all__ = [
    "REGISTERED_LOCAL_SERVICES",
    "ServiceProbeResult",
    "probe_registered_local_service",
]
