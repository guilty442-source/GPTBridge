"""integration executors — 結構性通道／同步匯流（stdlib；無決策層協調）。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..governance.delegation import ExecutorBinding

_channels: dict[str, dict[str, str]] = {}
_bus: dict[str, list[dict[str, Any]]] = defaultdict(list)


def _channel_register(payload: dict[str, Any]) -> dict[str, Any]:
    channel = str(payload.get("channel") or "")
    kind = str(payload.get("kind") or "structural")
    if not channel or channel in _channels:
        return {"registered": False, "channel": channel, "reason": "missing-or-duplicate"}
    _channels[channel] = {"kind": kind, "owner": "integration-sovereign"}
    return {"registered": True, "channel": channel, "kind": kind}


def _bus_publish(payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("event") or "")
    if not event:
        return {"published": False, "reason": "empty-event"}
    _bus[event].append({"seq": len(_bus[event]) + 1, "source": "integration-bus"})
    return {"published": True, "event": event, "seq": len(_bus[event])}


def _bus_subscribe(payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("event") or "")
    history = list(_bus.get(event, []))
    return {"subscribed": bool(event), "event": event, "pending": history}


def _iface_sync(payload: dict[str, Any]) -> dict[str, Any]:
    interface = str(payload.get("interface") or "")
    return {
        "interface": interface,
        "sync": "structural-only",
        "decision_coordination": "none",
        "executor": "iface-sync",
    }


def bindings() -> list[ExecutorBinding]:
    return [
        ExecutorBinding(
            executor_id="channel-register",
            boundary="structural-channel",
            permission_intent="channel-register",
            owner_sovereign="integration",
            target="integration-sovereign:integration:channel-register",
            implementation=_channel_register,
        ),
        ExecutorBinding(
            executor_id="bus-publish",
            boundary="sync-bus",
            permission_intent="bus-publish",
            owner_sovereign="integration",
            target="integration-sovereign:integration:bus-publish",
            implementation=_bus_publish,
        ),
        ExecutorBinding(
            executor_id="bus-subscribe",
            boundary="sync-bus",
            permission_intent="bus-subscribe",
            owner_sovereign="integration",
            target="integration-sovereign:integration:bus-subscribe",
            implementation=_bus_subscribe,
        ),
        ExecutorBinding(
            executor_id="iface-sync",
            boundary="structural-interface",
            permission_intent="iface-sync",
            owner_sovereign="integration",
            target="integration-sovereign:integration:iface-sync",
            implementation=_iface_sync,
        ),
    ]


__all__ = ["bindings"]