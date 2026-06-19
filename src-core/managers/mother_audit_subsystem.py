"""Compatibility import for the rescue mode subsystem.

New code should import from modes.rescue.service.
"""

from modes.rescue.service import MotherAuditSubsystem

__all__ = ["MotherAuditSubsystem"]

