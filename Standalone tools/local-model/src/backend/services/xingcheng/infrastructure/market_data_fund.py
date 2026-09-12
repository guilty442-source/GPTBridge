from __future__ import annotations

import re
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from .market_data_helpers import (
    FUNDCLEAR_BASE,
    FUNDCLEAR_SEARCH_URL,
    _currency_code,
    _fund_query_terms,
    _infer_frequency,
    _normalized_identifier,
    _normalized_name,
    _number,
    utc_now,
)


class MarketDataFundMixin:
    """Fund (mutual fund) search via FundClear."""

    def _search_fund(self, holding: dict[str, Any]) -> dict[str, Any]:
        name = str(holding.get("name") or "").strip()
        symbol = str(holding.get("symbol") or "").strip().upper()
        currency = str(holding.get("currency") or "").upper()
        fund_code = _normalized_identifier(holding.get("fund_code"))
        isin = _normalized_identifier(holding.get("fund_isin") or holding.get("isin"))
        if not name and not fund_code and not isin:
            return {"ok": False, "message": "共同基金缺少名稱、基金代碼或 ISIN，無法辨識級別"}
        candidates: list[dict[str, Any]] = []
        used_terms: list[str] = []
        for term in _fund_query_terms(name, fund_code=fund_code, isin=isin):
            used_terms.append(term)
            try:
                response = self.fetch_json(
                    FUNDCLEAR_SEARCH_URL,
                    {
                        "_pageNum": 1,
                        "_pageSize": 25,
                        "column": "",
                        "asc": False,
                        "fundSite": "all",
                        "searchKey": term,
                        "fundTypeList": ["all"],
                        "currencyList": ["all"],
                        "asiFreqList": ["all"],
                        "fundRiskLevelList": ["all"],
                    },
                )
            except urllib.error.HTTPError as error:
                if error.code not in {400, 404}:
                    raise
                response = {}
            candidates = [item for item in response.get("data", []) if isinstance(item, dict)]
            if candidates:
                break
        if not candidates:
            return {"ok": False, "message": f"基金資訊觀測站找不到：{name}"}
        wanted = _normalized_name(name)
        wants_distribution = any(token in name for token in ("配息", "月配", "週配", "季配", "入息", "收益"))

        def score(item: dict[str, Any]) -> float:
            candidate_name = str(item.get("fundName") or "")
            ratio = SequenceMatcher(None, wanted, _normalized_name(candidate_name)).ratio()
            candidate_code = _normalized_identifier(item.get("fundCode"))
            candidate_isin = _normalized_identifier(
                item.get("isin") or item.get("fundIsin") or item.get("ISIN")
            )
            if fund_code and candidate_code == fund_code:
                ratio = max(ratio, 0.98)
            if isin and candidate_isin == isin:
                ratio = max(ratio, 0.99)
            candidate_currency = _currency_code(str(item.get("currencyName") or ""))
            if currency and candidate_currency == currency:
                ratio += 0.12
            declared = str(item.get("asiFreq") or "")
            if wants_distribution and ("配" in declared or "分配" in declared) and "不配息" not in declared:
                ratio += 0.1
            if not wants_distribution and ("不配息" in declared or "不分配" in declared):
                ratio += 0.04
            return ratio

        best = max(candidates, key=score)
        confidence = min(0.99, score(best))
        if confidence < 0.62:
            return {"ok": False, "message": f"基金名稱匹配可信度不足：{name}"}
        official_code = str(best.get("fundCode") or "")
        fund_site = str(best.get("fundSite") or "").lower()
        observed_date = str(best.get("navTxnDate") or "").replace("/", "-")
        observed_at = f"{observed_date}T00:00:00+08:00" if observed_date else utc_now()
        source_url = f"{FUNDCLEAR_BASE}/fund-basic-info?" + urllib.parse.urlencode(
            {"key": official_code, "site": fund_site, "tab": "fund-nav"}
        )
        declared_frequency = str(best.get("asiFreq") or "")
        distribution_events = self._fund_distribution_evidence(official_code, fund_site)
        frequency = _infer_frequency(distribution_events, declared_frequency)
        nav = _number(best.get("navValue"), 0)
        parameters: dict[str, Any] = {
            "price": nav,
            "ytd_return_percent": best.get("changeRatio"),
            "distribution_frequency": frequency["code"],
        }
        return {
            "ok": nav > 0,
            "identity_key": f"fundclear:{fund_site}:{official_code}",
            "requested_symbol": symbol,
            "requested_name": name,
            "resolved_symbol": official_code,
            "official_code": official_code,
            "isin": str(holding.get("isin") or ""),
            "name": str(best.get("fundName") or name),
            "market": "FUND",
            "asset_type": "FUND",
            "currency": _currency_code(str(best.get("currencyName") or currency)),
            "quote_kind": "nav",
            "observed_at": observed_at,
            "confidence": round(confidence, 4),
            "trusted": confidence >= 0.72 and nav > 0,
            "parameters": parameters,
            "parameter_units": {"price": "currency", "ytd_return_percent": "%"},
            "distribution": {
                "frequency": frequency["code"],
                "frequency_label": frequency["label"],
                "frequency_per_year": frequency["per_year"],
                "frequency_confidence": frequency["confidence"],
                "declared_text": declared_frequency,
                "event_count": len(distribution_events),
            },
            "distribution_events": distribution_events,
            "sources": [{"name": "基金資訊觀測站", "url": source_url, "kind": "official-fund-nav", "observed_at": observed_at}],
            "search_terms": used_terms,
            "identity_resolution": {
                "method": (
                    "exact-isin"
                    if isin
                    and _normalized_identifier(
                        best.get("isin") or best.get("fundIsin") or best.get("ISIN")
                    )
                    == isin
                    else "exact-fund-code"
                    if fund_code and _normalized_identifier(best.get("fundCode")) == fund_code
                    else "normalized-name-and-share-class"
                ),
                "confidence": round(confidence, 4),
                "candidate_count": len(candidates),
                "searched_terms": used_terms,
            },
        }

    def _fund_distribution_evidence(self, code: str, site: str) -> list[dict[str, Any]]:
        if not code:
            return []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=365 * 5)
        endpoint = f"{FUNDCLEAR_BASE}/api/{'offshore' if site == 'offshore' else 'onshore'}/fund-basic/query-latest-nav"
        response = self.fetch_json(
            endpoint,
            {
                "fundCode": code,
                "startDate": start.strftime("%Y/%m"),
                "endDate": end.strftime("%Y/%m"),
                "column": "",
                "asc": False,
                "_pageNum": 1,
                "_pageSize": 60,
            },
        )
        events: list[dict[str, Any]] = []
        for item in response.get("list", []):
            if not isinstance(item, dict):
                continue
            raw_date = str(item.get("infoDate") or "")
            observed = raw_date
            if re.fullmatch(r"\d{8}", raw_date):
                observed = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}T00:00:00+08:00"
            url = str(item.get("infoUrl") or "")
            title = str(item.get("infoContent") or "")
            if not raw_date.strip() and not title.strip():
                continue
            events.append({
                "observed_at": observed,
                "record_date": raw_date,
                "title": title,
                "amount_per_unit": None,
                "currency": "",
                "frequency": "",
                "source_name": "基金資訊觀測站／投信投顧公會",
                "source_url": url,
                "evidence_only": True,
            })
        return events
