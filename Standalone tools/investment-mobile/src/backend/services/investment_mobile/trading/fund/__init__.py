"""fund domain — 共同基金資料引擎與 AI 基金投資分析.

Live fund trading is disabled this phase: analysis + recommendations
only. Providers are honest about verification state; controlled file
import is the entry point until an official API is verified.
"""

from .comparison import FundComparisonService
from .contracts import (
    DistributionPolicy,
    DistributionSource,
    FeeCalc,
    FeeKind,
    FundClassification,
    FundDistribution,
    FundFee,
    FundHolding,
    FundIdentity,
    FundNAV,
    FundRecommendation,
    FundStrategy,
    FundTransaction,
    FundTransactionType,
    FundTxnStatus,
    NavType,
    RecommendationType,
)
from .cost import FundCostBasisService
from .distribution import FundDistributionService
from .engine import MutualFundEngine
from .exposure import FundExposureEngine, UnifiedPortfolioExposure
from .fees import FundFeeEngine
from .identity import FundIdentityRegistry
from .maintenance import FundMaintenance
from .nav import FundNAVService
from .performance import FundPerformanceEngine
from .providers import PROVIDER_CAPABILITIES
from .recommendation import FundRecommendationService
from .recurring import FundRecurringInvestmentService
from .strategy import FundStrategyEngine
from .transactions import FundTransactionService

__all__ = (
    "DistributionPolicy",
    "DistributionSource",
    "FeeCalc",
    "FeeKind",
    "FundClassification",
    "FundComparisonService",
    "FundCostBasisService",
    "FundDistribution",
    "FundDistributionService",
    "FundExposureEngine",
    "FundFee",
    "FundFeeEngine",
    "FundHolding",
    "FundIdentity",
    "FundIdentityRegistry",
    "FundMaintenance",
    "FundNAV",
    "FundNAVService",
    "FundPerformanceEngine",
    "FundRecommendation",
    "FundRecommendationService",
    "FundRecurringInvestmentService",
    "FundStrategy",
    "FundStrategyEngine",
    "FundTransaction",
    "FundTransactionService",
    "FundTransactionType",
    "FundTxnStatus",
    "MutualFundEngine",
    "NavType",
    "PROVIDER_CAPABILITIES",
    "RecommendationType",
    "UnifiedPortfolioExposure",
)
