"""Pure AI Investment Manager domain contracts and rules."""

from .contract import (
    INVESTMENT_ANALYTICS_SCHEMA_VERSION,
    INVESTMENT_APP_VERSION,
    INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION,
    INVESTMENT_STATE_SCHEMA_VERSION,
    upgrade_compatibility_contract,
)

__all__ = (
    "INVESTMENT_ANALYTICS_SCHEMA_VERSION",
    "INVESTMENT_APP_VERSION",
    "INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION",
    "INVESTMENT_STATE_SCHEMA_VERSION",
    "upgrade_compatibility_contract",
)
