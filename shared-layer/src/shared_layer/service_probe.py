from __future__ import annotations

import socket
from dataclasses import asdict, dataclass
from typing import Final


REGISTERED_LOCAL_SERVICES: Final[dict[str, tuple[str, int]]] = {
    "postgresql": ("127.0.0.1", 5432),
    "qdrant": ("127.0.0.1", 6333),
    "ollama": ("127.0.0.1", 11434),
}


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
    """Probe a registered dependency through the information-layer boundary."""
    normalized = str(service or "").strip().casefold()
    endpoint = REGISTERED_LOCAL_SERVICES.get(normalized)
    if endpoint is None:
        raise PermissionError("UNREGISTERED_INFORMATION_CHANNEL")
    host, port = endpoint
    try:
        with socket.create_connection((host, port), timeout=max(0.05, timeout)):
            reachable = True
    except OSError:
        reachable = False
    return ServiceProbeResult(normalized, host, port, reachable)


__all__ = [
    "REGISTERED_LOCAL_SERVICES",
    "ServiceProbeResult",
    "probe_registered_local_service",
]
