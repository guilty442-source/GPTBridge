"""InvestmentOpportunityScanner — deterministic filter first, model
second.

Filters are user-configured (market/sector/volume/momentum/volatility/
valuation/strategy-signal/fund metrics). Every result reports which
conditions matched, which did not, what data was used and whether the
data was complete — an AI confidence number can never substitute for
strategy validation.
"""

from __future__ import annotations

from typing import Any

from ..intelligence.indicators import IndicatorSet

FILTER_KEYS = frozenset({
    "market", "sector", "min_volume", "min_momentum_pct",
    "max_volatility", "min_rsi", "max_rsi", "min_price", "max_price",
    "trend",                      # up | down | above_ma200
    "max_fee_pct", "min_fund_return_pct",
})


class InvestmentOpportunityScanner:
    def __init__(self, candle_store: Any, instruments: Any,
                 fund_engine: Any | None = None) -> None:
        self._candles = candle_store
        self._instruments = instruments
        self._fund = fund_engine

    def scan(self, universe: list[str],
             criteria: dict[str, Any]) -> dict[str, Any]:
        unknown = set(criteria) - FILTER_KEYS
        if unknown:
            return {"ok": False, "error_code": "FILTER_UNKNOWN",
                    "unknown": sorted(unknown)}
        results = []
        for iid in universe:
            rows = self._candles.candles(iid, "1d")[-220:]
            closes = [float(r.close) for r in rows]
            vols = [float(r.volume) for r in rows]
            meta = {}
            try:
                meta = self._instruments.get(iid) or {}
            except Exception:
                pass
            matched, missed, evidence = [], [], []
            data_complete = bool(rows)
            if not rows:
                results.append({"instrument_id": iid,
                                "matched": False,
                                "data_complete": False,
                                "missed": ["NO_DATA"]})
                continue
            ind = IndicatorSet(closes, vols)
            evidence.append(f"candles:{len(rows)}")
            for key, want in criteria.items():
                ok = self._check(key, want, ind, closes, vols, meta)
                (matched if ok else missed).append(key)
            results.append({
                "instrument_id": iid,
                "matched": not missed,
                "matched_conditions": matched,
                "missed_conditions": missed,
                "data_complete": data_complete,
                "evidence": evidence,
                "indicators": {k: v for k, v in ind.to_dict().items()
                               if v is not None},
            })
        return {"ok": True, "results": results,
                "note": "deterministic filters only — model annotates, "
                        "never fabricates a confidence score"}

    def _check(self, key: str, want: Any, ind: IndicatorSet,
               closes: list[float], vols: list[float],
               meta: dict[str, Any]) -> bool:
        try:
            if key == "market":
                return str(meta.get("market", "")) == str(want)
            if key == "sector":
                return str(meta.get("sector", "")) == str(want)
            if key == "min_volume":
                return vols and vols[-1] >= float(want)
            if key == "min_momentum_pct":
                return (ind.cum_return is not None
                        and ind.cum_return * 100 >= float(want))
            if key == "max_volatility":
                return (ind.volatility is not None
                        and ind.volatility <= float(want))
            if key == "min_rsi":
                return ind.rsi14 is not None and ind.rsi14 >= float(want)
            if key == "max_rsi":
                return ind.rsi14 is not None and ind.rsi14 <= float(want)
            if key == "min_price":
                return closes[-1] >= float(want)
            if key == "max_price":
                return closes[-1] <= float(want)
            if key == "trend":
                w = str(want)
                if w == "up":
                    return (ind.ma20 is not None and ind.ma50 is not None
                            and closes[-1] > ind.ma20 > ind.ma50)
                if w == "down":
                    return (ind.ma20 is not None and ind.ma50 is not None
                            and closes[-1] < ind.ma20 < ind.ma50)
                if w == "above_ma200":
                    return (ind.ma200 is not None
                            and closes[-1] > ind.ma200)
                return False
            if key == "max_fee_pct" and self._fund is not None:
                rules = self._fund.fees.rules(
                    fund_id=str(meta.get("fund_id", iid)))
                mgmt = [r for r in rules if "management" in r.kind
                        or "經理" in r.kind]
                return bool(mgmt) and all(
                    float(r.rate) * 100 <= float(want) for r in mgmt)
            if key == "min_fund_return_pct":
                return False    # requires fund performance data hookup
        except Exception:
            return False
        return False
