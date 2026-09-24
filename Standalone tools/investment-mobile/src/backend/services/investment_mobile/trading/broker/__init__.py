"""Independent broker adapters.

Every adapter starts unverified: until the official broker trading API is
obtained and verified, order dispatch fails closed with
``BROKER_API_UNVERIFIED``. Market data and simulation stay available so
analysis, SHADOW and PAPER modes are fully functional.
"""

from .base import BrokerAdapter, BrokerRegistry, broker_registry
from .cathay_tw import CathaySecuritiesAdapter
from .fubon_us import FubonSubBrokerageAdapter
from .fund_platform import FundPlatformAdapter

__all__ = (
    "BrokerAdapter",
    "BrokerRegistry",
    "CathaySecuritiesAdapter",
    "FubonSubBrokerageAdapter",
    "FundPlatformAdapter",
    "broker_registry",
)
