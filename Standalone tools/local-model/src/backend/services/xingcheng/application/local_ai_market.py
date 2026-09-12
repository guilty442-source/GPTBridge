from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from typing import Any


class LocalAiMarketMixin:
    @staticmethod
    def _native_persistence_requested(
        prompt: str, payload: dict[str, Any]
    ) -> bool:
        if payload.get("save_to_native_memory") is True:
            return True
        normalized = str(prompt or "").strip().casefold()
        return any(
            marker in normalized
            for marker in (
                "請記住",
                "幫我記住",
                "保存這",
                "儲存這",
                "存到記憶",
                "寫入記憶",
                "remember this",
                "save this",
                "store this",
            )
        )

    @staticmethod
    def _search_key(payload: dict[str, Any]) -> str:
        market_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"allow_external_fallback", "force_refresh"}
        }
        encoded = json.dumps(
            market_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _search_cache_ttl(payload: dict[str, Any]) -> int:
        requested = payload.get("holdings")
        if not isinstance(requested, list):
            requested = [payload.get("holding") or payload]
        holdings = [item for item in requested if isinstance(item, dict)]
        all_funds = bool(holdings) and all(
            str(item.get("asset_type") or item.get("market") or "").upper() == "FUND"
            for item in holdings
        )
        return 900 if all_funds else 60

    async def _search_market_data(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bool, int]:
        started = time.perf_counter()
        self._runtime_metrics["search_request_count"] = int(
            self._runtime_metrics["search_request_count"]
        ) + 1
        self._runtime_metrics["last_activity_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        key = self._search_key(payload)
        now = time.monotonic()
        ttl = self._search_cache_ttl(payload)
        force_refresh = payload.get("force_refresh") is True
        owner = False
        async with self._search_lock:
            expired = [cache_key for cache_key, (until, _) in self._search_cache.items() if until <= now]
            for cache_key in expired:
                self._search_cache.pop(cache_key, None)
            cached = self._search_cache.get(key)
            if cached and not force_refresh:
                self._runtime_metrics["search_cache_hit_count"] = int(
                    self._runtime_metrics["search_cache_hit_count"]
                ) + 1
                self._record_latency("search", started)
                return copy.deepcopy(cached[1]), True, ttl
            task = self._search_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    asyncio.to_thread(self.market_data.search, dict(payload))
                )
                self._search_inflight[key] = task
                owner = True
            else:
                self._runtime_metrics["search_cache_hit_count"] = int(
                    self._runtime_metrics["search_cache_hit_count"]
                ) + 1
        try:
            result = await task
        finally:
            if owner:
                async with self._search_lock:
                    self._search_inflight.pop(key, None)
        if owner:
            async with self._search_lock:
                self._search_cache[key] = (time.monotonic() + ttl, copy.deepcopy(result))
                while len(self._search_cache) > self.MAX_SEARCH_CACHE_ENTRIES:
                    self._search_cache.pop(next(iter(self._search_cache)))
        self._record_latency("search", started)
        return copy.deepcopy(result), not owner, ttl
