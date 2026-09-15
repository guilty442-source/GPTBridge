"""Sovereign Lifecycle Mixin — start/stop and supervision lifecycle."""

from __future__ import annotations

from typing import Any


class LifecycleMixin:
    """Mixin providing sovereign lifecycle management."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._started = False
        self._state: dict[str, Any] = {}

    @property
    def area(self) -> str:
        raise NotImplementedError("Subclass must implement 'area' property")

    @property
    def role(self) -> str:
        raise NotImplementedError("Subclass must implement 'role' property")

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> dict[str, Any]:
        """启动主宰（决策层初始化，不执行业务逻辑）。"""
        if self._started:
            return {"role": self.role, "status": "already_started"}

        self._state = {
            "role": self.role,
            "area": self.area,
            "started_at": self._iso_now(),
            "execution_delegation": "governed-executor-only",
        }
        self._started = True
        await self._on_start()
        return self._state

    async def stop(self) -> None:
        """停止主宰。"""
        if not self._started:
            return
        await self._on_stop()
        self._started = False
        self._state = {"stopped_at": self._iso_now()}

    async def _on_start(self) -> None:
        """子类覆写：启动时的额外初始化。"""
        pass

    async def start_supervision(self) -> None:
        """Start observation/supervision loops (A297 separation).

        The sovereign's ``start()`` only initializes the decision layer.
        Supervision/automation loops are started separately by the
        governed executor calling this method after ``start()`` returns,
        so the decision/observe boundary is explicit: the sovereign
        decides, the executor starts the observation work.
        """
        pass

    async def stop_supervision(self) -> None:
        """Stop observation/supervision loops (A297 separation)."""
        pass

    async def _on_stop(self) -> None:
        """子类覆写：停止时的清理。"""
        pass


__all__ = ["LifecycleMixin"]