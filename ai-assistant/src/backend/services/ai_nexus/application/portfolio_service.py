from __future__ import annotations

from .portfolio_svc_base import PortfolioSvcBaseMixin
from .portfolio_svc_dividends import PortfolioSvcDividendsMixin, local_device_now
from .portfolio_svc_fund import PortfolioSvcFundMixin
from .portfolio_svc_holdings import PortfolioSvcHoldingsMixin
from .portfolio_svc_ledger import PortfolioSvcLedgerMixin
from .portfolio_svc_sync import PortfolioSvcSyncMixin


class InvestmentPortfolioServiceMixin(
    PortfolioSvcBaseMixin,
    PortfolioSvcHoldingsMixin,
    PortfolioSvcLedgerMixin,
    PortfolioSvcSyncMixin,
    PortfolioSvcDividendsMixin,
    PortfolioSvcFundMixin,
):
    pass
