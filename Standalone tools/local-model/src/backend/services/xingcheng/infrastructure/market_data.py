from __future__ import annotations

import threading
from typing import Any

# Re-export helper constants and functions for backward compatibility.
from .market_data_helpers import (  # noqa: F401
    FetchJson,
    FUNDCLEAR_BASE,
    FUNDCLEAR_SEARCH_URL,
    MARKET_SUFFIXES,
    NETWORK_DESTINATION_ALLOWLIST,
    SUPPORTED_QUOTE_TYPES,
    TPEX_BASE,
    TWSE_BASE,
    YAHOO_SEARCH_URL,
    _currency_code,
    _fetch_json,
    _fund_query_terms,
    _infer_frequency,
    _normalized_identifier,
    _normalized_name,
    _number,
    _roc_date_iso,
    _yahoo_symbol_candidates,
    market_source_catalog,
    recognize_holding_identity,
    utc_now,
)
from .market_data_cache import MarketDataCacheMixin
from .market_data_fund import MarketDataFundMixin
from .market_data_search import MarketDataSearchMixin
from .market_data_tw_official import MarketDataTwOfficialMixin
from .market_data_yahoo import MarketDataYahooMixin


class MarketDataSearch(
    MarketDataCacheMixin,
    MarketDataSearchMixin,
    MarketDataTwOfficialMixin,
    MarketDataFundMixin,
    MarketDataYahooMixin,
):
    """星澄的唯讀市場搜尋層；不讀取投資管家資料庫。"""

    MAX_HOLDING_CACHE_ENTRIES = 32

    def __init__(self, fetch_json: FetchJson | None = None) -> None:
        self.fetch_json = fetch_json or _fetch_json
        self._holding_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._holding_cache_lock = threading.Lock()
        self._dataset_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._dataset_cache_lock = threading.Lock()


__all__ = [
    "MarketDataSearch",
    "FetchJson",
    "FUNDCLEAR_BASE",
    "FUNDCLEAR_SEARCH_URL",
    "TWSE_BASE",
    "TPEX_BASE",
    "YAHOO_SEARCH_URL",
    "NETWORK_DESTINATION_ALLOWLIST",
    "SUPPORTED_QUOTE_TYPES",
    "MARKET_SUFFIXES",
    "utc_now",
    "_number",
    "_fetch_json",
    "_normalized_name",
    "_normalized_identifier",
    "recognize_holding_identity",
    "_fund_query_terms",
    "_yahoo_symbol_candidates",
    "_currency_code",
    "_roc_date_iso",
    "_infer_frequency",
    "market_source_catalog",
]
