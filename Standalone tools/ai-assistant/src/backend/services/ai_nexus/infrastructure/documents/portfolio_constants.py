"""Module-level constants for the investment portfolio tooling."""

from __future__ import annotations

from datetime import time as local_time


PROGRESS_JSON_PREFIX = "INVESTMENT_MANAGER_PROGRESS_JSON="
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 12
DEFAULT_PROVIDER_ORDER = (
    "twse",
    "yahoo-chart",
    "yahoo-quote",
    "coingecko",
    "alphavantage",
)
DEFAULT_IMPORT_SNAPSHOT_KEEP = 20
SNAPSHOT_COPY_ATTEMPTS = 3
SNAPSHOT_COPY_CHUNK_BYTES = 1024 * 1024
XLSX_INFERRED_HEADER_SCAN_ROWS = 25
XLSX_HEADER_SCAN_MAX_ROWS = 80
XLSX_HEADER_SCAN_MAX_COLUMNS = 96
XLSX_HORIZONTAL_GROUP_WIDTH = 4
CSV_EXTENSIONS = {".csv", ".tsv", ".txt"}
JSON_EXTENSIONS = {".json"}
XLSX_EXTENSIONS = {".xlsx"}
LEGACY_EXCEL_EXTENSIONS = {".xls"}
EXCEL_EXTENSIONS = XLSX_EXTENSIONS | LEGACY_EXCEL_EXTENSIONS
COMMON_US_ETF_SYMBOLS = {
    "ARKK",
    "DIA",
    "EEM",
    "GLD",
    "IWM",
    "QQQ",
    "SCHD",
    "SLV",
    "SPY",
    "TLT",
    "VIG",
    "VOO",
    "VT",
    "VTI",
    "VXUS",
    "XLE",
    "XLF",
    "XLK",
}
CRYPTO_ID_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "USDT": "tether",
    "USDC": "usd-coin",
}
HEADER_ALIASES = {
    "symbol": {
        "symbol",
        "ticker",
        "code",
        "stockcode",
        "security",
        "securities",
        "isin",
        "stock",
        "stockid",
        "securityid",
        "productid",
        "instrument",
        "\u4ee3\u865f",
        "\u4ee3\u78bc",
        "\u80a1\u7968",
        "\u80a1\u7968\u4ee3\u865f",
        "\u80a1\u7968\u4ee3\u78bc",
        "\u80a1\u865f",
        "\u8b49\u5238\u4ee3\u865f",
        "\u8b49\u5238\u4ee3\u78bc",
        "\u5546\u54c1\u4ee3\u865f",
        "\u5546\u54c1\u4ee3\u78bc",
        "\u6a19\u7684",
        "\u6a19\u7684\u4ee3\u865f",
        "\u6a19\u7684\u4ee3\u78bc",
    },
    "name": {
        "name",
        "securityname",
        "stockname",
        "productname",
        "instrumentname",
        "\u540d\u7a31",
        "\u80a1\u540d",
        "\u8b49\u5238\u540d\u7a31",
        "\u80a1\u7968\u540d\u7a31",
        "\u5546\u54c1\u540d\u7a31",
        "\u6a19\u7684\u540d\u7a31",
    },
    "market": {
        "market",
        "exchange",
        "region",
        "\u5e02\u5834",
        "\u5e02\u5834\u5225",
        "\u4ea4\u6613\u6240",
        "\u4ea4\u6613\u5e02\u5834",
        "\u5340\u57df",
    },
    "asset_type": {
        "type",
        "assettype",
        "category",
        "assetclass",
        "\u985e\u578b",
        "\u5546\u54c1\u985e\u578b",
        "\u8cc7\u7522\u985e\u5225",
        "\u8cc7\u7522\u985e\u578b",
    },
    "quantity": {
        "quantity",
        "qty",
        "shares",
        "units",
        "position",
        "holding",
        "balance",
        "availablequantity",
        "\u80a1\u6578",
        "\u5f35\u6578",
        "\u55ae\u4f4d",
        "\u5eab\u5b58",
        "\u5eab\u5b58\u6578\u91cf",
        "\u5eab\u5b58\u80a1\u6578",
        "\u6301\u80a1",
        "\u6301\u6709\u80a1\u6578",
        "\u6301\u6709\u6578\u91cf",
        "\u6578\u91cf",
    },
    "average_cost": {
        "averagecost",
        "avgcost",
        "averageprice",
        "avgprice",
        "cost",
        "costbasisprice",
        "price",
        "unitcost",
        "\u6210\u672c",
        "\u6210\u672c\u50f9",
        "\u55ae\u4f4d\u6210\u672c",
        "\u8cb7\u9032\u6210\u672c",
        "\u5e73\u5747\u6210\u672c",
        "\u5e73\u5747\u6210\u672c\u50f9",
        "\u5e73\u5747\u50f9",
        "\u5e73\u5747\u8cb7\u9032\u6210\u672c",
        "\u6210\u4ea4\u5747\u50f9",
        "\u8cb7\u9032\u5747\u50f9",
        "\u5747\u50f9",
    },
    "currency": {"currency", "ccy", "\u5e63\u5225", "\u4ea4\u6613\u5e63\u5225", "\u8ca8\u5e63"},
    "principal_amount": {
        "principal",
        "principalamount",
        "costamount",
        "\u672c\u91d1",
        "\u6295\u5165\u672c\u91d1",
        "\u6295\u5165\u91d1\u984d",
        "\u6210\u672c\u91d1\u984d",
    },
    "principal_currency": {
        "principalcurrency",
        "principalccy",
        "\u672c\u91d1\u5e63\u5225",
        "\u6210\u672c\u5e63\u5225",
    },
    "principal_twd": {
        "principaltwd",
        "\u672c\u91d1twd",
        "\u53f0\u5e63\u672c\u91d1",
        "\u65b0\u53f0\u5e63\u672c\u91d1",
    },
}
XLSX_MAPPING_FIELDS = tuple(HEADER_ALIASES)
XLSX_REQUIRED_MAPPING_FIELDS = frozenset({"symbol", "quantity"})
MARKET_ALIASES = {
    "US": {"us", "usa", "nyse", "nasdaq", "amex", "\u7f8e\u80a1", "\u7f8e\u570b"},
    "TW": {
        "tw",
        "tpe",
        "twse",
        "tpex",
        "taiwan",
        "\u53f0\u80a1",
        "\u81fa\u80a1",
        "\u53f0\u7063",
    },
    "HK": {"hk", "hkg", "hkex", "hongkong", "\u6e2f\u80a1", "\u9999\u6e2f"},
    "CRYPTO": {
        "crypto",
        "coin",
        "\u52a0\u5bc6",
        "\u865b\u64ec\u8ca8\u5e63",
        "\u52a0\u5bc6\u8ca8\u5e63",
    },
    "FUND": {"fund", "mutualfund", "\u57fa\u91d1"},
    "INDEX": {"index", "indice", "indices", "\u6307\u6578"},
}
MARKET_SESSIONS = {
    "US": {
        "timezone": "America/New_York",
        "sessions": [(local_time(9, 30), local_time(16, 0))],
    },
    "TW": {
        "timezone": "Asia/Taipei",
        "sessions": [(local_time(9, 0), local_time(13, 30))],
    },
    "HK": {
        "timezone": "Asia/Hong_Kong",
        "sessions": [
            (local_time(9, 30), local_time(12, 0)),
            (local_time(13, 0), local_time(16, 0)),
        ],
    },
}
