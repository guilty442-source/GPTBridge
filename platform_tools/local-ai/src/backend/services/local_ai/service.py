from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .external_research import ExternalBrowserResearch
from .investment_analysis import analyze_investments
from .market_data import MarketDataSearch
from .native_model import StarNativeLanguageModel
from .repository import LocalAiRepository
from .upgrade_evaluation import evaluate_star_upgrade


class LocalAiService:
    VERSION = "1.0.0"
    COMMANDS = {
        "local_ai_status",
        "local_ai_infer",
        "local_ai_search_investments",
        "local_ai_analyze_investments",
        "local_ai_evaluate_upgrade",
    }

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()
        self.repository = LocalAiRepository(self.tool_root)
        self.market_data = MarketDataSearch()
        self.native_model = StarNativeLanguageModel()
        self.external_research = ExternalBrowserResearch()
        self.model = self.native_model.MODEL_ID
        self._search_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._search_inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._search_lock = asyncio.Lock()

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
                return copy.deepcopy(cached[1]), True, ttl
            task = self._search_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    asyncio.to_thread(self.market_data.search, dict(payload))
                )
                self._search_inflight[key] = task
                owner = True
        try:
            result = await task
        finally:
            if owner:
                async with self._search_lock:
                    self._search_inflight.pop(key, None)
        if owner:
            async with self._search_lock:
                self._search_cache[key] = (time.monotonic() + ttl, copy.deepcopy(result))
        return copy.deepcopy(result), not owner, ttl

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        return None

    async def handle(
        self, command: str, payload: dict[str, Any], _latest: Any = None
    ) -> tuple[str, dict[str, Any]]:
        if command == "local_ai_status":
            database = self.repository.database_status()
            evaluation = evaluate_star_upgrade(
                version=self.VERSION,
                tool_root=self.tool_root,
                database=database,
                external_research_configured=self.external_research.configured(),
                star_native_model_enabled=True,
                external_model_enabled=False,
            )
            result = {
                "ok": True,
                "name": "星澄",
                "model": self.model,
                "status": "星澄原生本地模型已就緒（不使用外部模型）",
                "model_version": self.native_model.VERSION,
                "model_architecture": self.native_model.ARCHITECTURE,
                "model_mode": "native-language-model",
                "external_model_used": False,
                "network_access": "public-investment-sources-read-only",
                "endpoint_scope": "none-native-engine",
                "market_search": "public-web-read-only",
                "investment_analysis_owner": "星澄",
                "external_research": {
                    "configured": self.external_research.configured(),
                    "enabled": self.external_research.configured(),
                    "policy": "explicit-need-only",
                    "role": "supplemental-discussion-not-model-inference",
                    "transport": "browser-session-via-authenticated-websocket",
                    "uses_api": False,
                    "queue_when_offline": False,
                },
                "database": database,
                "upgrade_optimization": evaluation,
            }
            return "local_ai_status_result", result
        if command == "local_ai_evaluate_upgrade":
            result = evaluate_star_upgrade(
                version=self.VERSION,
                tool_root=self.tool_root,
                database=self.repository.database_status(),
                external_research_configured=self.external_research.configured(),
                star_native_model_enabled=True,
                external_model_enabled=False,
            )
            return "local_ai_evaluate_upgrade_result", result
        if command == "local_ai_search_investments":
            result, cache_hit, cache_ttl = await self._search_market_data(dict(payload))
            result["cache"] = {
                "hit": cache_hit,
                "ttl_seconds": cache_ttl,
                "coalesced": cache_hit and payload.get("force_refresh") is True,
            }
            if result.get("errors") and payload.get("allow_external_fallback") is True:
                result["external_research"] = await asyncio.to_thread(
                    self.external_research.search,
                    [
                        {
                            "symbol": str(item.get("symbol") or ""),
                            "name": str(item.get("name") or ""),
                            "message": str(item.get("message") or ""),
                        }
                        for item in result.get("errors", [])
                        if isinstance(item, dict)
                    ],
                )
                result["external_research"]["role"] = (
                    "supplemental-collaboration-only"
                )
                result["external_research"]["used_by_native_model"] = False
            if not cache_hit:
                await asyncio.to_thread(
                    self.repository.record_market_search, dict(payload), result
                )
            return "local_ai_search_investments_result", result
        if command == "local_ai_analyze_investments":
            result = await asyncio.to_thread(analyze_investments, dict(payload))
            self.repository.record("star-investment-analysis", payload, result)
            return "local_ai_analyze_investments_result", result
        if command != "local_ai_infer":
            raise ValueError(f"unsupported local AI command: {command}")
        output = await asyncio.to_thread(
            self.native_model.infer,
            dict(payload),
            database=self.repository.database_status(),
            analyze=analyze_investments,
            search=self.market_data.search,
        )
        if not output.get("ok"):
            return "local_ai_infer_result", output
        market_research = output.get("market_research")
        if isinstance(market_research, dict):
            await asyncio.to_thread(
                self.repository.record_market_search,
                {
                    "holdings": payload.get("holdings") or [],
                    "origin": "star-native-language-model",
                },
                market_research,
            )
        self.repository.record(
            self.model,
            {"prompt": str(payload.get("prompt") or "")},
            output,
        )
        return "local_ai_infer_result", output
