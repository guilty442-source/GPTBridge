"""Xingcheng Control Surface Package — A366/A380.

A366: CONTROL-SURFACE: 星澄 auxiliary system owns the user-facing control surface for
automatic repair and automatic update. Its release controls apply only to system repair
and system update. Xingcheng self-upgrade is an owned-domain internal lifecycle and is
never controlled by these switches.

A380: 星澄 auxiliary system provides two independent user-operated manual release switches:
repair_release and update_release. The user alone decides whether each switch is enabled;
default is disabled.
"""

from .surface import (
    SwitchState,
    SwitchStatus,
    ControlSurfaceStatus,
    SwitchStore,
    XingchengControlSurface,
    REPAIR_RELEASE_SWITCH,
    UPDATE_RELEASE_SWITCH,
    create_xingcheng_control_surface,
)

from .commands import XingchengCommandHandler

__all__ = [
    # Surface
    "SwitchState",
    "SwitchStatus",
    "ControlSurfaceStatus",
    "SwitchStore",
    "XingchengControlSurface",
    "REPAIR_RELEASE_SWITCH",
    "UPDATE_RELEASE_SWITCH",
    "create_xingcheng_control_surface",
    # Commands
    "XingchengCommandHandler",
]
