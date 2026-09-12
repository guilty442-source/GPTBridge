from __future__ import annotations

import copy
import json
import time
from typing import Any


class MarketDataCacheMixin:
    """Cache management for market data search results."""

    @staticmethod
    def _holding_cache_key(holding: dict[str, Any]) -> str:
        return json.dumps(
            {
                key: holding.get(key)
                for key in (
                    "symbol",
                    "name",
                    "market",
                    "asset_type",
                    "currency",
                    "fund_quote_symbol",
                    "fund_code",
                    "fund_isin",
                    "isin",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _holding_cache_ttl(result: dict[str, Any]) -> int:
        distribution = result.get("distribution")
        if isinstance(distribution, dict) and distribution.get("frequency") == "none":
            return 86400
        if str(result.get("asset_type") or "").upper() == "FUND":
            return 900
        return 60

    def release_idle_resources(self, now: float | None = None) -> None:
        checked_at = time.monotonic() if now is None else float(now)
        with self._holding_cache_lock:
            self._holding_cache = {
                key: value
                for key, value in self._holding_cache.items()
                if value[0] > checked_at
            }
        with self._dataset_cache_lock:
            self._dataset_cache = {
                key: value
                for key, value in self._dataset_cache.items()
                if value[0] > checked_at
            }

    def _cached_dataset(self, url: str, ttl_seconds: int) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._dataset_cache_lock:
            cached = self._dataset_cache.get(url)
            if cached and cached[0] > now:
                return copy.deepcopy(cached[1])
            payload = self.fetch_json(url, None)
            rows = payload.get("data") if isinstance(payload, dict) else []
            dataset = [dict(item) for item in rows if isinstance(item, dict)]
            self._dataset_cache[url] = (now + max(30, ttl_seconds), dataset)
            return copy.deepcopy(dataset)

    def _search_holding_cached(
        self, holding: dict[str, Any], *, force_refresh: bool = False
    ) -> dict[str, Any]:
        key = self._holding_cache_key(holding)
        now = time.monotonic()
        with self._holding_cache_lock:
            cached = self._holding_cache.get(key)
            if cached and cached[0] > now and not force_refresh:
                result = copy.deepcopy(cached[1])
                result["cache"] = {
                    "hit": True,
                    "ttl_policy": "no-distribution-daily"
                    if self._holding_cache_ttl(result) == 86400
                    else "market-data",
                }
                return result
            if cached and cached[0] <= now:
                self._holding_cache.pop(key, None)
        result = self._search_holding(holding)
        if result.get("ok"):
            ttl = self._holding_cache_ttl(result)
            with self._holding_cache_lock:
                self._holding_cache[key] = (now + ttl, copy.deepcopy(result))
                expired = [
                    cache_key
                    for cache_key, (until, _cached) in self._holding_cache.items()
                    if until <= now
                ]
                for cache_key in expired:
                    self._holding_cache.pop(cache_key, None)
                while len(self._holding_cache) > self.MAX_HOLDING_CACHE_ENTRIES:
                    self._holding_cache.pop(next(iter(self._holding_cache)))
            result["cache"] = {
                "hit": False,
                "ttl_seconds": ttl,
                "ttl_policy": "no-distribution-daily" if ttl == 86400 else "market-data",
            }
        return result
