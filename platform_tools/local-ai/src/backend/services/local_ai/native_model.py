from __future__ import annotations

import re
from typing import Any, Callable


InvestmentAnalyzer = Callable[[dict[str, Any]], dict[str, Any]]
MarketSearcher = Callable[[dict[str, Any]], dict[str, Any]]


class StarNativeLanguageModel:
    """First-party language-model pipeline with deterministic v1 decoding."""

    MODEL_ID = "star-native-language-model"
    VERSION = "1.0"
    ARCHITECTURE = (
        "star-tokenizer+intent-encoder+local-retrieval+tool-router+response-decoder"
    )

    _INTENTS = (
        ("distribution", ("配息", "股息", "收益分配", "除息")),
        ("quote", ("報價", "價格", "淨值", "行情")),
        ("risk", ("風險", "波動", "回撤", "集中", "壓力")),
        ("analysis", ("分析", "評估", "投資", "持股", "資產")),
        ("status", ("狀態", "健康", "資料庫", "版本", "模型")),
    )

    @classmethod
    def _intent(cls, prompt: str) -> str:
        normalized = "".join(cls._tokenize(prompt)).casefold()
        for intent, tokens in cls._INTENTS:
            if any(token.casefold() in normalized for token in tokens):
                return intent
        return "capabilities"

    @staticmethod
    def _tokenize(prompt: str) -> list[str]:
        normalized = str(prompt or "").strip()
        return re.findall(r"[\u3400-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?|[^\s]", normalized)

    @staticmethod
    def _database_evidence(database: dict[str, Any]) -> list[dict[str, Any]]:
        tables = database.get("tables") if isinstance(database.get("tables"), dict) else {}
        quality = database.get("quality") if isinstance(database.get("quality"), dict) else {}
        return [
            {
                "id": "star-database:instrument-identities",
                "value": int(tables.get("instrument_identity") or 0),
            },
            {
                "id": "star-database:market-observations",
                "value": int(tables.get("market_observation") or 0),
            },
            {
                "id": "star-database:distribution-events",
                "value": int(tables.get("distribution_event") or 0),
            },
            {
                "id": "star-database:invalid-distribution-events",
                "value": int(quality.get("blank_distribution_events") or 0),
            },
        ]

    def infer(
        self,
        payload: dict[str, Any],
        *,
        database: dict[str, Any],
        analyze: InvestmentAnalyzer,
        search: MarketSearcher,
    ) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return {
                "ok": False,
                "error_code": "PROMPT_REQUIRED",
                "message": "請輸入要查詢或分析的內容。",
            }

        intent = self._intent(prompt)
        holdings = payload.get("holdings")
        market_research: dict[str, Any] | None = None
        network_allowed = payload.get("allow_network") is not False
        if (
            network_allowed
            and isinstance(holdings, list)
            and holdings
            and intent in {"distribution", "quote", "risk", "analysis"}
        ):
            market_research = search(
                {
                    "holdings": holdings,
                    "require_all": False,
                    "max_workers": payload.get("max_workers") or 8,
                }
            )
        analysis: dict[str, Any] | None = None
        if isinstance(holdings, list) and holdings:
            analysis = analyze(
                {
                    "holdings": holdings,
                    "analysis_parameters": payload.get("analysis_parameters") or {},
                }
            )

        if intent == "distribution":
            response = (
                "星澄會依公開來源區分「無配息」與「資料不足」。已確認不配息的標的會顯示無配息，"
                "並使用每日負快取，避免持續重複查詢；需要時可由星澄直接查詢公開網站。"
            )
        elif intent == "quote":
            response = (
                "報價與基金淨值由星澄的公開來源搜尋模組取得，數值必須附來源與觀測時間；"
                "本地模型不猜測即時價格。"
            )
        elif intent in {"risk", "analysis"} and analysis is not None:
            warnings = analysis.get("risk_warnings")
            warning_count = len(warnings) if isinstance(warnings, list) else 0
            response = (
                f"星澄已使用自己的投資參數引擎完成分析，共辨識 {warning_count} 項風險提醒。"
                "結論來自輸入持股與星澄資料庫，不使用外部模型。"
            )
        elif intent in {"risk", "analysis"}:
            response = (
                "請從投資管家帶入持股後執行分析。星澄會計算集中度、幣別曝險、配息與風險參數，"
                "資料不足時會明確保留，不補造結論。"
            )
        elif intent == "status":
            tables = database.get("tables") if isinstance(database.get("tables"), dict) else {}
            response = (
                "星澄原生本地模型已就緒，不使用 Ollama、第三方或外部模型。"
                f"目前保存 {int(tables.get('instrument_identity') or 0)} 個標的識別、"
                f"{int(tables.get('market_observation') or 0)} 筆市場觀測。"
            )
        else:
            response = (
                "我是星澄原生本地模型。目前提供投資資料搜尋、配息判定、參數化分析、"
                "資料品質檢查與自動修正；可查詢公開網路來源，但不使用第三方生成模型，"
                "也不會猜測缺少的投資事實。"
            )

        return {
            "ok": True,
            "model": self.MODEL_ID,
            "model_version": self.VERSION,
            "architecture": self.ARCHITECTURE,
            "mode": "native-language-model",
            "pipeline": [
                "star-tokenizer",
                "intent-encoder",
                "local-retrieval",
                "investment-tool-router",
                "deterministic-response-decoder",
            ],
            "token_count": len(self._tokenize(prompt)),
            "intent": intent,
            "response": response,
            "analysis": analysis,
            "market_research": market_research,
            "evidence": self._database_evidence(database),
            "external_model_used": False,
            "network_used_for_inference": market_research is not None,
            "network_scope": "public-investment-sources-read-only"
            if network_allowed
            else "disabled-by-request",
            "facts_locked": True,
            "training_policy": "star-owned-local-data-only",
            "third_party_weights_used": False,
        }
