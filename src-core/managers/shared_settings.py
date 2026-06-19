"""Compatibility import for the settings subsystem.

New code should import from modes.settings.service.
"""

from modes.settings.service import SharedSettingsManager

__all__ = ["SharedSettingsManager"]

