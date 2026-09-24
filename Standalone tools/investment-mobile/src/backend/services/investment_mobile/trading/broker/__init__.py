"""Independent broker adapters.

Every adapter starts unverified: until the official broker trading API is
obtained and verified, order dispatch fails closed with
``BROKER_API_UNVERIFIED``. Market data and simulation stay available so
analysis, SHADOW and PAPER modes are fully functional.
"""

from .base import BrokerAdapter, BrokerRegistry
from .cathay_tw import CathayTwAdapter
from .fubon_us import FubonUsAdapter
from .fund_platform import FundPlatformAdapter

__all__ = (
    "BrokerAdapter",
    "BrokerRegistry",
    "CathayTwAdapter",
    "FubonUsAdapter",
    "FundPlatformAdapter",
)
