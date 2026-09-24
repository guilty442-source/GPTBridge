"""Six core business domains of 星澄 AI 投資管理與自動操盤系統."""

from .ai_analysis import AiAnalysisDomain
from .asset_mgmt import AssetManagementDomain
from .auto_trading import AutoTradingDomain
from .base import BusinessDomain
from .fund import FundDomain
from .tw_stock import TwStockDomain
from .us_stock import UsStockDomain

__all__ = (
    "AiAnalysisDomain",
    "AssetManagementDomain",
    "AutoTradingDomain",
    "BusinessDomain",
    "FundDomain",
    "TwStockDomain",
    "UsStockDomain",
)


def build_domains() -> list[BusinessDomain]:
    return [
        TwStockDomain(),
        UsStockDomain(),
        FundDomain(),
        AiAnalysisDomain(),
        AutoTradingDomain(),
        AssetManagementDomain(),
    ]
