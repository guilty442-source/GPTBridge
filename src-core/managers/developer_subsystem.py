"""Compatibility import for the developer mode subsystem.

New code should import from modes.developer.service.
"""

from modes.developer.service import DeveloperSubsystem

__all__ = ["DeveloperSubsystem"]

