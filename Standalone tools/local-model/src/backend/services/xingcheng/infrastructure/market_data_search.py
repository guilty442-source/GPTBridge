from __future__ import annotations

import copy
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Any

from .market_data_helpers import (
    MARKET_SUFFIXES,
    SUPPORTED_QUOTE_TYPES,
    YAHOO_SEARCH_URL,
    _normalized_identifier,
    _normalized_name,
    _number,
    market_source_catalog,
    recognize_holding_identity,
    utc_now,
)


class MarketDataSearchMixin:
    """Search orchestration and identity resolution for market data."""

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("holdings")
        if not isinstance(requested, list):
            requested = [payload.get("holding") or payload]
        holdings = [
            recognize_holding_identity(dict(item))
            for item in requested
            if isinstance(item, dict)
        ][:300]
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        workers = max(1, min(int(payload.get("max_workers") or 4), 6))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._search_holding_cached,
                    holding,
                    force_refresh=payload.get("force_refresh") is True,
                ): holding
                for holding in holdings
            }
            for future in as_completed(futures):
                holding = futures[future]
                try:
                    result = future.result()
                    if result.get("ok"):
                        results.append(result)
                    else:
                        errors.append(
                            {
                                "symbol": str(holding.get("symbol") or ""),
                                "name": str(holding.get("name") or ""),
                                "market": str(holding.get("market") or ""),
                                "asset_type": str(holding.get("asset_type") or ""),
                                "message": str(result.get("message") or "找不到可驗證資料"),
                                "recognition_status": str(
                                    (holding.get("recognition") or {}).get("status") or ""
                                ),
                            }
                        )
                except Exception as error:
                    errors.append(
                        {
                            "symbol": str(holding.get("symbol") or ""),
                            "name": str(holding.get("name") or ""),
                            "market": str(holding.get("market") or ""),
                            "asset_type": str(holding.get("asset_type") or ""),
                            "message": f"{type(error).__name__}: {error}",
                            "recognition_status": "failed",
                        }
                    )
        results.sort(key=lambda item: (str(item.get("market")), str(item.get("requested_symbol"))))
        return {
            "ok": bool(results) and not (payload.get("require_all") and errors),
            "provider": "星澄即時網路搜尋",
            "searched_at": utc_now(),
            "queued": False,
            "requested_count": len(holdings),
            "updated_count": len(results),
            "error_count": len(errors),
            "results": results,
            "errors": errors[:50],
            "requested_fields": ["dividend", "price", "nav"],
            "source_catalog": market_source_catalog(),
            "data_policy": "source-attributed; unresolved or conflicting values are not invented",
            "recognition": {
                "recognized_count": sum(
                    1
                    for item in holdings
                    if (item.get("recognition") or {}).get("status") == "recognized"
                ),
                "unresolved_count": len(errors),
                "strategy": "normalized-identity-exact-code-name-search-and-source-verification",
            },
        }

    def _search_holding(self, holding: dict[str, Any]) -> dict[str, Any]:
        holding = recognize_holding_identity(holding)
        asset_type = str(holding.get("asset_type") or "").upper()
        market = str(holding.get("market") or "").upper()
        symbol = str(holding.get("symbol") or "").strip().upper()
        if asset_type == "FUND" or market == "FUND" or symbol.startswith("FUND-"):
            return self._with_verification(self._search_fund(holding), holding)
        yahoo = self._search_yahoo(holding)
        if yahoo.get("ok") is not True and holding.get("name"):
            resolved = self._resolve_yahoo_identity(holding)
            if resolved.get("ok") is True:
                holding = {
                    **holding,
                    "symbol": resolved["resolved_symbol"],
                    "market": resolved.get("market") or market,
                    "asset_type": resolved.get("asset_type") or asset_type,
                }
                market = str(holding.get("market") or "").upper()
                yahoo = self._search_yahoo(holding)
                if yahoo.get("ok") is True:
                    yahoo["identity_resolution"] = resolved
        if market != "TW":
            return self._with_verification(yahoo, holding)
        official = self._search_tw_official(holding)
        if official.get("ok") is not True:
            return self._with_verification(yahoo, holding)
        if yahoo.get("ok") is not True:
            return self._with_verification(official, holding)
        return self._with_verification(
            self._merge_tw_official_result(yahoo, official), holding
        )

    def _resolve_yahoo_identity(self, holding: dict[str, Any]) -> dict[str, Any]:
        name = str(holding.get("name") or "").strip()
        if not name:
            return {"ok": False, "message": "缺少可辨識名稱"}
        url = YAHOO_SEARCH_URL + "?" + urllib.parse.urlencode(
            {
                "q": name,
                "quotesCount": 12,
                "newsCount": 0,
                "enableFuzzyQuery": "true",
            }
        )
        payload = self.fetch_json(url, None)
        raw_quotes = payload.get("quotes") if isinstance(payload, dict) else []
        candidates: list[dict[str, Any]] = []
        expected_name = _normalized_name(name)
        expected_market = str(holding.get("market") or "").upper()
        expected_currency = str(holding.get("currency") or "").upper()
        for raw in raw_quotes if isinstance(raw_quotes, list) else []:
            if not isinstance(raw, dict):
                continue
            quote_type = str(raw.get("quoteType") or "").upper()
            symbol = _normalized_identifier(raw.get("symbol"))
            if not symbol or quote_type not in SUPPORTED_QUOTE_TYPES:
                continue
            candidate_name = str(raw.get("longname") or raw.get("shortname") or symbol)
            score = SequenceMatcher(
                None, expected_name, _normalized_name(candidate_name)
            ).ratio()
            candidate_market = self._market_from_yahoo_candidate(raw, symbol)
            if expected_market and candidate_market == expected_market:
                score += 0.14
            candidate_currency = str(raw.get("currency") or "").upper()
            if expected_currency and candidate_currency == expected_currency:
                score += 0.08
            candidates.append(
                {
                    "symbol": symbol,
                    "name": candidate_name,
                    "market": candidate_market,
                    "asset_type": "FUND" if quote_type == "MUTUALFUND" else quote_type,
                    "currency": candidate_currency,
                    "exchange": str(raw.get("exchange") or raw.get("exchDisp") or ""),
                    "confidence": round(min(0.99, score), 4),
                }
            )
        candidates.sort(key=lambda item: float(item["confidence"]), reverse=True)
        best = candidates[0] if candidates else None
        if best is None or float(best["confidence"]) < 0.64:
            return {
                "ok": False,
                "message": "名稱搜尋沒有足夠可信的標的",
                "candidate_count": len(candidates),
            }
        return {
            "ok": True,
            "resolved_symbol": best["symbol"],
            "resolved_name": best["name"],
            "market": best["market"],
            "asset_type": best["asset_type"],
            "currency": best["currency"],
            "confidence": best["confidence"],
            "source": "Yahoo Finance Search",
            "source_url": url,
            "candidates": candidates[:5],
        }

    @staticmethod
    def _market_from_yahoo_candidate(raw: dict[str, Any], symbol: str) -> str:
        for suffix, market in sorted(
            MARKET_SUFFIXES.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if symbol.endswith(suffix):
                return market
        exchange = str(raw.get("exchange") or raw.get("exchDisp") or "").upper()
        if exchange in {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "NASDAQ", "NYSE"}:
            return "US"
        return ""

    @staticmethod
    def _with_verification(
        result: dict[str, Any], holding: dict[str, Any]
    ) -> dict[str, Any]:
        if result.get("ok") is not True:
            return result
        enriched = copy.deepcopy(result)
        sources = [
            item for item in enriched.get("sources", []) if isinstance(item, dict)
        ]
        official = sum(
            1
            for item in sources
            if str(item.get("kind") or "").startswith("official-")
        )
        source_count = len(
            {
                (str(item.get("name") or ""), str(item.get("url") or ""))
                for item in sources
            }
        )
        recognition = holding.get("recognition")
        enriched["recognition"] = (
            dict(recognition) if isinstance(recognition, dict) else {}
        )
        enriched["verification"] = {
            "source_count": source_count,
            "official_source_count": official,
            "cross_checked": source_count >= 2,
            "grade": "A" if official and source_count >= 2 else "B" if official else "C",
            "manual_update_allowed": bool(enriched.get("trusted")) and (
                official > 0 or _number(enriched.get("confidence")) >= 0.78
            ),
        }
        return enriched
