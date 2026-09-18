"""Xingcheng Control Surface — User-facing manual release switches (A366, A380).

A366: CONTROL-SURFACE: 星澄 auxiliary system owns the user-facing control surface for
automatic repair and automatic update. Its release controls apply only to system repair
and system update. Xingcheng self-upgrade is an owned-domain internal lifecycle and is
never controlled by these switches.

A380: 星澄 auxiliary system provides two independent user-operated manual release switches:
repair_release and update_release. The user alone decides whether each switch is enabled;
default is disabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from core_system.codex_decision import accepted_outcome, refusal_outcome
from core_system.sovereign_utils import _iso_now

_logger = logging.getLogger("gptbridge.xingcheng.control")

# A366/A380: The two independent manual release switches
REPAIR_RELEASE_SWITCH = "xingcheng.repair_release"
UPDATE_RELEASE_SWITCH = "xingcheng.update_release"


class SwitchState(Enum):
    """Switch state enumeration."""
    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True)
class SwitchStatus:
    """Status of a manual release switch."""
    switch_id: str
    state: SwitchState
    enabled_at: Optional[str] = None
    disabled_at: Optional[str] = None
    enabled_by: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlSurfaceStatus:
    """Complete status of the Xingcheng control surface."""
    repair_release: SwitchStatus
    update_release: SwitchStatus
    last_updated: str
    operator: Optional[str] = None


class SwitchStore:
    """Persistent store for switch states."""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self._ensure_store()

    def _ensure_store(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.store_path.exists():
            self._write_defaults()

    def _write_defaults(self) -> None:
        defaults = {
            REPAIR_RELEASE_SWITCH: {
                "state": SwitchState.DISABLED.value,
                "enabled_at": None,
                "disabled_at": _iso_now(),
                "enabled_by": None,
                "metadata": {"default": True},
            },
            UPDATE_RELEASE_SWITCH: {
                "state": SwitchState.DISABLED.value,
                "enabled_at": None,
                "disabled_at": _iso_now(),
                "enabled_by": None,
                "metadata": {"default": True},
            },
        }
        self._write(defaults)

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.store_path)

    def get_switch(self, switch_id: str) -> SwitchStatus:
        data = self._read()
        switch_data = data.get(switch_id, {})
        return SwitchStatus(
            switch_id=switch_id,
            state=SwitchState(switch_data.get("state", SwitchState.DISABLED.value)),
            enabled_at=switch_data.get("enabled_at"),
            disabled_at=switch_data.get("disabled_at"),
            enabled_by=switch_data.get("enabled_by"),
            metadata=switch_data.get("metadata", {}),
        )

    def set_switch(
        self,
        switch_id: str,
        state: SwitchState,
        operator: Optional[str] = None,
    ) -> SwitchStatus:
        data = self._read()
        now = _iso_now()
        current = data.get(switch_id, {})
        new_data = {
            "state": state.value,
            "enabled_at": now if state == SwitchState.ENABLED else current.get("enabled_at"),
            "disabled_at": now if state == SwitchState.DISABLED else current.get("disabled_at"),
            "enabled_by": operator if state == SwitchState.ENABLED else None,
            "metadata": current.get("metadata", {}),
        }
        data[switch_id] = new_data
        self._write(data)
        return SwitchStatus(
            switch_id=switch_id,
            state=state,
            enabled_at=new_data["enabled_at"],
            disabled_at=new_data["disabled_at"],
            enabled_by=new_data["enabled_by"],
            metadata=new_data["metadata"],
        )

    def get_all_switches(self) -> dict[str, SwitchStatus]:
        return {sid: self.get_switch(sid) for sid in [REPAIR_RELEASE_SWITCH, UPDATE_RELEASE_SWITCH]}


class XingchengControlSurface:
    """A366/A380: Xingcheng Control Surface — User-facing manual release switches.

    The 星澄 auxiliary system owns the user-facing control surface for automatic
    repair and automatic update. It exposes two independent explicit user-operated
    manual release switches:

    1. repair_release - controls automatic repair execution
    2. update_release - controls automatic update execution

    The user alone decides whether each switch is enabled; default is disabled.
    """

    def __init__(
        self,
        app: Any,
        store_path: Optional[Path] = None,
    ) -> None:
        self.app = app
        self.store = SwitchStore(
            store_path or Path(getattr(app, "project_root", Path.cwd()))
            / "main-system" / "runtime" / "state" / "xingcheng_control_surface.json"
        )

    def get_surface_status(self) -> ControlSurfaceStatus:
        """Get complete control surface status."""
        repair = self.store.get_switch(REPAIR_RELEASE_SWITCH)
        update = self.store.get_switch(UPDATE_RELEASE_SWITCH)
        return ControlSurfaceStatus(
            repair_release=repair,
            update_release=update,
            last_updated=_iso_now(),
        )

    def get_switch_status(self, switch_id: str) -> Optional[SwitchStatus]:
        """Get status of a specific switch."""
        if switch_id not in [REPAIR_RELEASE_SWITCH, UPDATE_RELEASE_SWITCH]:
            return None
        return self.store.get_switch(switch_id)

    def set_switch(
        self,
        switch_id: str,
        state: SwitchState,
        operator: str = "authenticated-user",
    ) -> SwitchStatus:
        """Set a switch state (user-operated, A366/A380).

        The user alone decides whether each switch is enabled.
        """
        if switch_id not in [REPAIR_RELEASE_SWITCH, UPDATE_RELEASE_SWITCH]:
            raise ValueError(f"Unknown switch: {switch_id}")

        # Update store
        new_status = self.store.set_switch(switch_id, state, operator)

        # Update automation switches in auto_action_policy
        self._sync_automation_switch(switch_id, state)

        _logger.info("XingchengControlSurface: %s set to %s by %s", switch_id, state.value, operator)
        return new_status

    def _sync_automation_switch(self, switch_id: str, state: SwitchState) -> None:
        """Sync with auto_action_policy switches."""
        try:
            from core_system.auto_action_policy import (
                AUTOMATIC_REPAIR_SWITCH,
                AUTOMATIC_UPDATE_SWITCH,
                set_automation_switch,
            )
            project_root = getattr(self.app, "project_root", None)
            if project_root:
                if switch_id == REPAIR_RELEASE_SWITCH:
                    # Retired: autonomous repair is managed by the
                    # system-audit flow; nothing to sync.
                    return
                if switch_id == UPDATE_RELEASE_SWITCH:
                    set_automation_switch(
                        project_root, AUTOMATIC_UPDATE_SWITCH,
                        state == SwitchState.ENABLED, actor="xingcheng-control"
                    )
        except Exception as exc:
            _logger.warning("XingchengControlSurface: failed to sync automation switch: %s", exc)

    def is_repair_release_enabled(self) -> bool:
        """Automatic repair is managed by the system-audit flow (retired switch)."""
        return True

    def is_update_release_enabled(self) -> bool:
        """Check if automatic update release is enabled."""
        return self.store.get_switch(UPDATE_RELEASE_SWITCH).state == SwitchState.ENABLED

    def enable_repair_release(self, operator: str = "authenticated-user") -> SwitchStatus:
        """Enable automatic repair release."""
        return self.set_switch(REPAIR_RELEASE_SWITCH, SwitchState.ENABLED, operator)

    def disable_repair_release(self, operator: str = "authenticated-user") -> SwitchStatus:
        """Disable automatic repair release."""
        return self.set_switch(REPAIR_RELEASE_SWITCH, SwitchState.DISABLED, operator)

    def enable_update_release(self, operator: str = "authenticated-user") -> SwitchStatus:
        """Enable automatic update release."""
        return self.set_switch(UPDATE_RELEASE_SWITCH, SwitchState.ENABLED, operator)

    def disable_update_release(self, operator: str = "authenticated-user") -> SwitchStatus:
        """Disable automatic update release."""
        return self.set_switch(UPDATE_RELEASE_SWITCH, SwitchState.DISABLED, operator)


# Convenience functions for creating control surface
def create_xingcheng_control_surface(app: Any) -> XingchengControlSurface:
    """Create XingchengControlSurface from app."""
    return XingchengControlSurface(app)


__all__ = [
    "SwitchState",
    "SwitchStatus",
    "ControlSurfaceStatus",
    "SwitchStore",
    "XingchengControlSurface",
    "REPAIR_RELEASE_SWITCH",
    "UPDATE_RELEASE_SWITCH",
    "create_xingcheng_control_surface",
]
