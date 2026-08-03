from __future__ import annotations

import copy
from typing import Any


MARKET_SOURCE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "twse-openapi",
        "name": "臺灣證券交易所 OpenAPI",
        "url": "https://openapi.twse.com.tw/",
        "fields": ["price", "dividend"],
        "role": "structured-official",
    },
    {
        "id": "tpex-openapi",
        "name": "證券櫃檯買賣中心 OpenAPI",
        "url": "https://www.tpex.org.tw/openapi/",
        "fields": ["price", "dividend"],
        "role": "structured-official",
    },
    {
        "id": "fundclear",
        "name": "基金資訊觀測站",
        "url": "https://www.fundclear.com.tw/",
        "fields": ["nav", "dividend"],
        "role": "structured-official",
    },
    {
        "id": "mops",
        "name": "公開資訊觀測站",
        "url": "https://mops.twse.com.tw/",
        "fields": ["dividend"],
        "role": "official-verification",
    },
    {
        "id": "sitca",
        "name": "中華民國投信投顧公會",
        "url": "https://www.sitca.org.tw/",
        "fields": ["nav", "dividend"],
        "role": "official-verification",
    },
    {
        "id": "yahoo-finance",
        "name": "Yahoo Finance",
        "url": "https://finance.yahoo.com/",
        "fields": ["price", "dividend", "nav"],
        "role": "structured-public",
    },
    {
        "id": "morningstar",
        "name": "Morningstar",
        "url": "https://www.morningstar.com/",
        "fields": ["nav", "dividend"],
        "role": "verification-candidate",
    },
    {
        "id": "moneydj",
        "name": "MoneyDJ 理財網",
        "url": "https://www.moneydj.com/",
        "fields": ["price", "nav", "dividend"],
        "role": "verification-candidate",
    },
    {
        "id": "google-search",
        "name": "Google 搜尋",
        "url": "https://www.google.com/",
        "fields": ["price", "nav", "dividend"],
        "role": "discovery-only",
    },
    {
        "id": "hkex",
        "name": "香港交易所",
        "url": "https://www.hkex.com.hk/",
        "fields": ["price", "dividend"],
        "role": "official-verification-candidate",
    },
    {
        "id": "jpx",
        "name": "日本交易所集團",
        "url": "https://www.jpx.co.jp/english/",
        "fields": ["price", "dividend"],
        "role": "official-verification-candidate",
    },
    {
        "id": "sec-edgar",
        "name": "SEC EDGAR",
        "url": "https://www.sec.gov/edgar/search/",
        "fields": ["dividend"],
        "role": "official-verification-candidate",
    },
    {
        "id": "nasdaq",
        "name": "Nasdaq",
        "url": "https://www.nasdaq.com/market-activity/",
        "fields": ["price", "dividend"],
        "role": "verification-candidate",
    },
)


def market_source_catalog() -> list[dict[str, Any]]:
    return copy.deepcopy(list(MARKET_SOURCE_CATALOG))


__all__ = ["MARKET_SOURCE_CATALOG", "market_source_catalog"]
