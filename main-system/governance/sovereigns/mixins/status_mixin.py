"""Sovereign Status Mixin — canonical status reporting schema."""

from __future__ import annotations

from typing import Any


class StatusBase:
    """Mixin providing canonical sovereign-status schema."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sub_sovereigns: dict[str, Any] = {}
        self._child_failure_counts: dict[str, int] = {}



    @property
    def role(self) -> str:
        return self.sovereign_id

    def _iso_now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _with_status_schema(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Canonical sovereign-status schema (single shape across sovereigns).

        Common keys: schema marker, role/sovereign identity, area, started;
        domain-specific surfaces extend the same payload instead of inventing
        a new shape, so consumers can branch on ``schema``.
        """
        result: dict[str, Any] = {
            "schema": "gptbridge.sovereign-status/v1",
            "role": self.sovereign_id,
            "sovereign": self.sovereign_id,
            "area": self.area,
            "started": self._started,
            **dict(self._state),
        }
        if payload:
            result.update(payload)
        return result

    def status(self) -> dict[str, Any]:
        """状态回报（唯读）。"""
        return self._with_status_schema()

    def live_status(self) -> dict[str, Any]:
        """即时状态（供编排层查询）。"""
        return self.status()

    def orchestration_status(self) -> dict[str, Any]:
        """编排层状态（含子系统健康）。"""
        return {
            "state": "active" if self._started else "stopped",
            "owner": self.role,
            "children": {
                child_id: bool(getattr(child, "started", False))
                for child_id, child in self._sub_sovereigns.items()
            },
            "child_failure_counts": dict(self._child_failure_counts),
        }


__all__ = ["StatusBase"]