from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from shared_layer.runtime_gateway import InformationChannelGateway
from shared_layer.service_probe import probe_registered_local_service


def test_runtime_gateway_validates_queues_and_audits() -> None:
    audited: list[dict[str, object]] = []

    async def route(command: str, payload: dict[str, object]):
        return f"{command}_result", {"ok": True, **payload}

    async def scenario() -> None:
        gateway = InformationChannelGateway(route, audited.append)
        event, result = await gateway.dispatch(
            sender="authenticated-ui",
            destination="main-system",
            command="status",
            payload={"typed": True},
        )
        assert event == "status_result"
        assert result == {"ok": True, "typed": True}

    asyncio.run(scenario())
    assert audited[0]["transport_owner"] == "shared-layer"


def test_service_probe_rejects_unregistered_destination() -> None:
    try:
        probe_registered_local_service("private-bypass")
    except PermissionError as error:
        assert str(error) == "UNREGISTERED_INFORMATION_CHANNEL"
    else:
        raise AssertionError("unregistered service probe was accepted")
